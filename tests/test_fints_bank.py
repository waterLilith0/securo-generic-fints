import json
import re
import sys
import types
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fints_bank as p


class Amount:
    def __init__(self, amount, currency='EUR'):
        self.amount = Decimal(amount)
        self.currency = currency


class Bank:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.init_tan_response = None
        self.callbacks = []
        self.entries = [
            SimpleNamespace(iban='DE12123456780000000001'),
            SimpleNamespace(iban='DE12123456780000000002'),
        ]

    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False

    def get_sepa_accounts(self):
        return self.entries

    def get_information(self):
        return {'accounts': [
            {'iban': self.entries[0].iban, 'product_name': 'Girokonto', 'currency': 'EUR'},
            {'iban': self.entries[1].iban, 'product_name': 'Tagesgeld', 'currency': 'EUR'},
        ]}

    def get_balance(self, entry):
        return SimpleNamespace(amount=Amount('2300.34' if entry == self.entries[0] else '4100.32'))

    def get_transactions(self, entry, **kwargs):
        return [
            SimpleNamespace(data={
                'date': date(2026, 11, 17), 'purpose': 'REWE',
                'amount': Amount('-34.78'), 'applicant_name': 'REWE'}),
            SimpleNamespace(data={
                'date': date(2026, 11, 17), 'purpose': 'Gehalt',
                'amount': Amount('2600.00'), 'applicant_name': 'Arbeitgeber'}),
        ]

    def add_response_callback(self, callback):
        self.callbacks.append(callback)

    def remove_response_callback(self, callback):
        self.callbacks.remove(callback)

    def deconstruct(self):
        return b'not-an-actual-bank-blob'


class FakeTAN:
    def __init__(self, decoupled=False):
        self.decoupled = decoupled


TEST_TODAY = date(2026, 11, 18)


def context(saved=None, cfg=None):
    return SimpleNamespace(
        secrets={'login': 'test-login', 'pin': '12345', **(saved or {})},
        config=cfg or {},
    )


class TestPlugin(unittest.TestCase):
    def setUp(self):
        fake = types.ModuleType('fints')
        client = types.ModuleType('fints.client')
        client.FinTS3PinTanClient = Bank
        client.NeedTANResponse = FakeTAN
        self.mods = patch.dict(sys.modules, {'fints': fake, 'fints.client': client})
        self.mods.start()
        self.addCleanup(self.mods.stop)
        product_id = patch.object(p, 'PRODUCT_ID', 'registered-test-id')
        product_id.start()
        self.addCleanup(product_id.stop)

    def test_product_id_is_supplied_by_plugin_not_connection(self):
        observed = {}

        def factory(**kwargs):
            observed.update(kwargs)
            return Bank(**kwargs)

        p._sync({}, context(cfg={'product_id': 'ignored'}), factory=factory, today=TEST_TODAY)
        self.assertEqual(observed['product_id'], 'registered-test-id')

    def test_missing_plugin_product_id_stops_sync(self):
        with patch.object(p, 'PRODUCT_ID', ''), self.assertRaisesRegex(
                p.BankSyncError, 'Plugin product ID is not configured'):
            p._sync({}, context(), factory=Bank, today=TEST_TODAY)

    def test_contract_imports_multiple_accounts_from_one_connection(self):
        result = p._sync({}, context(), factory=Bank, today=date(2026, 11, 18))
        accounts = result['data']['accounts']
        self.assertEqual(result['data']['contract'], 1)
        self.assertEqual(len(accounts), 2)
        self.assertEqual({account['type'] for account in accounts}, {'checking', 'savings'})
        self.assertEqual(accounts[0]['masked_number'], '0001')
        self.assertEqual(accounts[0]['balance'], '2300.34')
        self.assertEqual(accounts[0]['transactions'][0]['type'], 'debit')
        self.assertEqual(accounts[0]['transactions'][0]['amount'], '34.78')
        self.assertNotIn('holdings', result['data'])
        self.assertNotIn('iban', json.dumps(result, ensure_ascii=False).lower())
        self.assertNotIn('12345', json.dumps(result))

    def test_account_selection_filters_and_rejects_missing_iban(self):
        selected = p._sync(
            {}, context(saved={'account_ibans': 'DE12 1234 5678 0000 0000 02'}),
            factory=Bank, today=TEST_TODAY)
        self.assertEqual(len(selected['data']['accounts']), 1)
        self.assertEqual(selected['data']['accounts'][0]['masked_number'], '0002')
        with self.assertRaisesRegex(p.BankSyncError, 'not returned'):
            p._sync({}, context(saved={'account_ibans': 'DE12123456780000000003'}),
                    factory=Bank, today=TEST_TODAY)

    def test_profile_and_bank_code_select_the_configured_endpoint(self):
        observed = {}

        def factory(**kwargs):
            observed.update(kwargs)
            return Bank(**kwargs)

        p._sync({}, context(cfg={
            'bank_profile': 'atruvia',
            'blz': '43060967',
        }), factory=factory, today=TEST_TODAY)
        self.assertEqual(observed['bank_identifier'], '43060967')
        self.assertEqual(observed['server'], 'https://fints2.atruvia.de/cgi-bin/hbciservlet')

    def test_researched_profiles_use_their_selected_endpoints(self):
        cases = {
            'atruvia-1': ('43060967', 'https://fints1.atruvia.de/cgi-bin/hbciservlet'),
            'gad': ('43060967', 'https://hbci-pintan.gad.de/cgi-bin/hbciservlet'),
            'comdirect': ('20041111', 'https://fints.comdirect.de/fints'),
            'deutsche-bank': ('10070000', 'https://fints.deutsche-bank.de/'),
            'dkb': ('12030000', 'https://fints.dkb.de/fints'),
        }
        for profile, (blz, server) in cases.items():
            with self.subTest(profile=profile):
                observed = {}

                def factory(**kwargs):
                    observed.update(kwargs)
                    return Bank(**kwargs)

                p._sync({}, context(cfg={'bank_profile': profile, 'blz': blz}),
                        factory=factory, today=TEST_TODAY)
                self.assertEqual(observed['bank_identifier'], blz)
                self.assertEqual(observed['server'], server)

    def test_bank_code_must_be_eight_digits(self):
        with self.assertRaisesRegex(p.BankSyncError, 'exactly 8 digits'):
            p._sync({}, context(cfg={
                'bank_profile': 'atruvia', 'blz': '4306096x'}), factory=Bank)

    def test_gls_profile_supplies_blz_and_uses_gls_endpoint(self):
        observed = {}

        def factory(**kwargs):
            observed.update(kwargs)
            return Bank(**kwargs)

        p._sync({}, context(cfg={'bank_profile': 'gls'}), factory=factory, today=TEST_TODAY)
        self.assertEqual(observed['bank_identifier'], '43060967')
        self.assertEqual(observed['server'], 'https://fints1.atruvia.de/cgi-bin/hbciservlet')
        with self.assertRaisesRegex(p.BankSyncError, 'must be 43060967'):
            p._sync({}, context(cfg={'bank_profile': 'gls', 'blz': '43060968'}), factory=Bank)

    def test_gls_profile_syncs_accounts_with_standard_fints_calls(self):
        observed = {'account_lists': 0, 'balances': [], 'transactions': []}

        class GLSBank(Bank):
            def get_sepa_accounts(self):
                observed['account_lists'] += 1
                entries = super().get_sepa_accounts()
                observed['listed_accounts'] = [entry.iban for entry in entries]
                return entries

            def get_balance(self, entry):
                observed['balances'].append(entry.iban)
                return super().get_balance(entry)

            def get_transactions(self, entry, **kwargs):
                observed['transactions'].append((entry.iban, kwargs))
                return super().get_transactions(entry, **kwargs)

        def factory(**kwargs):
            observed['client_kwargs'] = kwargs
            return GLSBank(**kwargs)

        result = p._sync(
            {}, context(cfg={'bank_profile': 'gls'}), factory=factory, today=TEST_TODAY)

        accounts = result['data']['accounts']
        self.assertEqual(observed['client_kwargs']['bank_identifier'], '43060967')
        self.assertEqual(observed['client_kwargs']['server'],
                         'https://fints1.atruvia.de/cgi-bin/hbciservlet')
        self.assertEqual(observed['account_lists'], 1)
        self.assertEqual(observed['balances'], observed['listed_accounts'])
        self.assertEqual(len(observed['transactions']), 2)
        self.assertTrue(all('include_pending' not in kwargs
                            for _, kwargs in observed['transactions']))
        self.assertEqual(len(accounts), 2)
        self.assertTrue(all(account['institution_name'] == 'GLS Bank' for account in accounts))
        self.assertTrue(all(account['institution_external_id'] == 'fints-43060967'
                            for account in accounts))
        self.assertTrue(all(len(account['transactions']) == 2 for account in accounts))

    def test_reject_unknown_profiles_and_missing_group_blz(self):
        with self.assertRaisesRegex(p.BankSyncError, 'Unknown bank profile'):
            p._sync({}, context(cfg={'bank_profile': 'arbitrary'}), factory=Bank)
        with self.assertRaisesRegex(p.BankSyncError, 'BLZ is required'):
            p._sync({}, context(cfg={'bank_profile': 'atruvia'}), factory=Bank)

    def test_bank_configuration_invalidates_saved_client_state(self):
        initial = p._sync({}, context(), factory=Bank, today=TEST_TODAY)
        observed = {}

        def factory(**kwargs):
            observed.update(kwargs)
            return Bank(**kwargs)

        p._sync({}, context(saved=initial['secrets'], cfg={
            'bank_profile': 'atruvia',
            'blz': '43060967',
        }), factory=factory, today=TEST_TODAY)
        self.assertNotIn('from_data', observed)

    def test_gls_endpoint_change_preserves_account_ids_without_restoring_old_client_state(self):
        old_profile = {**p.BANK_PROFILES['gls'],
                       'server': 'https://fints2.atruvia.de/cgi-bin/hbciservlet'}
        with patch.dict(p.BANK_PROFILES, {'gls': old_profile}):
            initial = p._sync({}, context(cfg={'bank_profile': 'gls'}), factory=Bank, today=TEST_TODAY)

        observed = {}

        def factory(**kwargs):
            observed.update(kwargs)
            return Bank(**kwargs)

        updated = p._sync({}, context(saved=initial['secrets'], cfg={'bank_profile': 'gls'}),
                          factory=factory, today=TEST_TODAY)
        self.assertNotIn('from_data', observed)
        self.assertEqual(updated['secrets']['id_salt'], initial['secrets']['id_salt'])
        self.assertEqual(updated['data'], initial['data'])

    def test_updated_bank_endpoints_discard_client_state_but_preserve_account_ids(self):
        cases = {
            'gad': ('43060967', 'https://hbci-pintan.gad.de/fints/'),
            'comdirect': ('20041111', 'https://fints.comdirect.de/fints/'),
            'deutsche-bank': ('10070000', 'https://fints.deutsche-bank.de/fints/'),
            'dkb': ('12030000', 'https://banking-dkb.s-fints-pt-dkb.de/fints/'),
        }
        for profile, (blz, old_server) in cases.items():
            with self.subTest(profile=profile):
                cfg = {'bank_profile': profile, 'blz': blz}
                old_profile = {**p.BANK_PROFILES[profile], 'server': old_server}
                with patch.dict(p.BANK_PROFILES, {profile: old_profile}):
                    initial = p._sync({}, context(cfg=cfg), factory=Bank, today=TEST_TODAY)

                observed = {}

                def factory(**kwargs):
                    observed.update(kwargs)
                    return Bank(**kwargs)

                updated = p._sync({}, context(saved=initial['secrets'], cfg=cfg),
                                  factory=factory, today=TEST_TODAY)
                self.assertNotIn('from_data', observed)
                self.assertEqual(updated['secrets']['id_salt'], initial['secrets']['id_salt'])
                self.assertEqual(updated['data'], initial['data'])

    def test_deterministic_ids_with_saved_salt(self):
        one = p._sync({}, context(), factory=Bank, today=date(2026, 11, 18))
        two = p._sync({}, context(saved=one['secrets']), factory=Bank, today=date(2026, 11, 18))
        self.assertEqual(one['data'], two['data'])
        self.assertEqual(one['secrets']['id_salt'], two['secrets']['id_salt'])
        self.assertEqual(one['data']['accounts'][0]['external_id'],
                         one['data']['accounts'][0]['stable_external_id'])
        self.assertEqual(
            one['data']['accounts'][0]['external_id'],
            p._digest(one['secrets']['id_salt'], 'ing-', 'DE12123456780000000001'))

    def test_incremental_window(self):
        observed = {}

        class Observing(Bank):
            def get_transactions(self, entry, **kwargs):
                observed['since'] = kwargs['start_date']
                return super().get_transactions(entry, **kwargs)

        p._sync({'since': '2026-11-04'}, context(), factory=Observing, today=date(2026, 11, 18))
        self.assertEqual(observed['since'], date(2026, 11, 4))

    def test_auth_errors_do_not_expose_bank_details(self):
        for kind, expected in [('FinTSClientPINError', 'FinTSClientPINError'),
                               ('FinTSClientTemporaryAuthError', '3938'),
                               ('FinTSSCARequiredError', '9075')]:
            error = type(kind, (Exception,), {})

            def fail(**kwargs):
                raise error('sensitive bank details')

            with self.subTest(kind=kind), self.assertRaises(p.NeedsAuth) as caught:
                p._sync({}, context(), factory=fail, today=TEST_TODAY)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn('sensitive bank details', str(caught.exception))

    def test_connection_errors_are_not_reported_as_bank_rate_limits(self):
        for kind in ('FinTSConnectionError', 'FinTSNoResponseError', 'TimeoutError',
                     'ConnectTimeout', 'ReadTimeout', 'ConnectionError'):
            error = type(kind, (Exception,), {})

            def fail(**kwargs):
                raise error('sensitive bank details')

            with self.subTest(kind=kind), self.assertRaises(p.BankSyncError) as caught:
                p._sync({}, context(cfg={'bank_profile': 'gls'}), factory=fail, today=TEST_TODAY)
            self.assertIn(kind, str(caught.exception))
            self.assertNotIn('sensitive bank details', str(caught.exception))

    def test_tan_challenge_pauses_sync(self):
        class Challenge(Bank):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.init_tan_response = FakeTAN()

        with self.assertRaises(p.NeedsAuth):
            p._sync({}, context(), factory=Challenge, today=TEST_TODAY)

        class BalanceChallenge(Bank):
            def get_balance(self, entry):
                return FakeTAN()

        with self.assertRaises(p.NeedsAuth):
            p._sync({}, context(), factory=BalanceChallenge, today=TEST_TODAY)

    def test_push_approval_resumes_each_read_stage(self):
        for stage in ('initialization', 'account list', 'account balance', 'transactions'):
            class PushBank(Bank):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self.pending = {}
                    self.polls = 0
                    if stage == 'initialization':
                        self.init_tan_response = FakeTAN(decoupled=True)
                        self.pending[id(self.init_tan_response)] = object()

                def get_current_tan_mechanism(self):
                    return 'push'

                def get_tan_mechanisms(self):
                    return {'push': SimpleNamespace(
                        automated_polling_allowed=True, decoupled_max_poll_number=3,
                        wait_before_first_poll=7, wait_before_next_poll=9)}

                def challenge(self, value):
                    response = FakeTAN(decoupled=True)
                    self.pending[id(response)] = value
                    return response

                def send_tan(self, challenge, tan):
                    self.assert_empty_tan = tan == ''
                    self.polls += 1
                    if self.polls % 2:
                        return challenge
                    return self.pending.pop(id(challenge))

                def get_sepa_accounts(self):
                    result = super().get_sepa_accounts()
                    return self.challenge(result) if stage == 'account list' else result

                def get_balance(self, entry):
                    result = super().get_balance(entry)
                    return self.challenge(result) if stage == 'account balance' else result

                def get_transactions(self, entry, **kwargs):
                    result = super().get_transactions(entry, **kwargs)
                    return self.challenge(result) if stage == 'transactions' else result

            banks = []

            def factory(**kwargs):
                bank = PushBank(**kwargs)
                banks.append(bank)
                return bank

            with self.subTest(stage=stage), patch.object(p.time, 'monotonic', return_value=0), \
                    patch.object(p.time, 'sleep') as sleep:
                result = p._sync({}, context(cfg={'bank_profile': 'gls'}), factory=factory, today=TEST_TODAY)
                self.assertEqual(len(result['data']['accounts']), 2)
                self.assertTrue(banks[0].assert_empty_tan)
                self.assertEqual(sleep.call_args_list[0].args, (7,))
                self.assertEqual(sleep.call_args_list[1].args, (9,))
                self.assertIsNone(banks[0].init_tan_response)
                self.assertEqual(banks[0].callbacks, [])

    def test_push_approval_respects_bank_limits_and_timeout(self):
        class PendingBank(Bank):
            def __init__(self, mechanism, **kwargs):
                super().__init__(**kwargs)
                self.mechanism = mechanism
                self.polls = 0
                self.init_tan_response = FakeTAN(decoupled=True)

            def get_current_tan_mechanism(self):
                return 'push'

            def get_tan_mechanisms(self):
                return {'push': self.mechanism}

            def send_tan(self, challenge, tan):
                self.polls += 1
                return challenge

        for mechanism, expected_polls, message in (
            (SimpleNamespace(decoupled_max_poll_number=1), 1, 'not completed'),
            (SimpleNamespace(wait_before_first_poll=40), 0, 'not completed'),
            (SimpleNamespace(automated_polling_allowed=False), 0, 'does not allow'),
        ):
            bank = PendingBank(mechanism)
            with self.subTest(message=message), patch.object(p.time, 'monotonic', return_value=0), \
                    patch.object(p.time, 'sleep'), self.assertRaisesRegex(p.NeedsAuth, message):
                p._sync({}, context(), factory=lambda **kwargs: bank, today=TEST_TODAY)
            self.assertEqual(bank.polls, expected_polls)

    def test_push_rejection_stops_sync_without_importing(self):
        class RejectedBank(Bank):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.init_tan_response = FakeTAN(decoupled=True)

            def get_current_tan_mechanism(self):
                return 'push'

            def get_tan_mechanisms(self):
                return {'push': SimpleNamespace()}

            def send_tan(self, challenge, tan):
                for callback in self.callbacks:
                    callback(None, SimpleNamespace(code='9999'))
                return object()

        bank = RejectedBank()
        with patch.object(p.time, 'monotonic', return_value=0), patch.object(p.time, 'sleep'), \
                self.assertRaisesRegex(p.NeedsAuth, 'rejected app approval'):
            p._sync({}, context(), factory=lambda **kwargs: bank, today=TEST_TODAY)
        self.assertEqual(bank.callbacks, [])

    def test_reject_empty_accounts_and_missing_secrets(self):
        class Empty(Bank):
            def get_sepa_accounts(self):
                return []

        with self.assertRaises(p.BankSyncError):
            p._sync({}, context(), factory=Empty, today=TEST_TODAY)
        with self.assertRaises(p.NeedsAuth):
            p._sync({}, context(saved={'pin': ''}), factory=Bank)

    def test_bad_history_and_amounts(self):
        with self.assertRaises(p.BankSyncError):
            p._sync({}, context(cfg={'history_days': '400'}), factory=Bank)
        with self.assertRaises(p.BankSyncError):
            p._money('nan')

    def test_duplicate_occurrence_ids_are_deterministic(self):
        transaction = SimpleNamespace(data={
            'date': date(2026, 11, 17), 'purpose': 'Test', 'amount': Amount('-2.35')})
        salt = 'ab' * 32
        first = p._transaction_rows([transaction, transaction], salt, 'account')
        second = p._transaction_rows([transaction, transaction], salt, 'account')
        self.assertEqual(first, second)
        self.assertEqual(len({row['external_id'] for row in first}), 2)

    def test_booking_date_precedes_value_date_and_results_use_no_floats(self):
        transaction = SimpleNamespace(data={
            'date': date(2026, 11, 15), 'entry_date': date(2026, 11, 17),
            'purpose': 'Kartenzahlung', 'amount': Amount('-4.20')})
        rows = p._transaction_rows([transaction], 'ab' * 32, 'account')
        self.assertEqual(rows[0]['date'], '2026-11-17')

        data = p._sync({}, context(), factory=Bank, today=TEST_TODAY)

        def assert_no_floats(value):
            if isinstance(value, dict):
                for child in value.values():
                    assert_no_floats(child)
            elif isinstance(value, list):
                for child in value:
                    assert_no_floats(child)
            else:
                self.assertNotIsInstance(value, float)

        assert_no_floats(data)

    def test_manifest_matches_code_allowlist_and_provider_contract(self):
        manifest_path = Path(__file__).resolve().parents[1] / 'securo-plugin.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertEqual(manifest['min_securo_version'], '0.20.0')
        self.assertEqual(manifest['runtime']['entrypoint'], 'fints_bank:plugin')
        self.assertEqual({entry['host'] for entry in manifest['network']}, p.ALLOWED_FINTS_HOSTS)
        profile_hosts = {profile['server'].split('/')[2] for profile in p.BANK_PROFILES.values()}
        self.assertTrue(profile_hosts <= p.ALLOWED_FINTS_HOSTS)
        integration = manifest['contributes']['integrations'][0]
        self.assertEqual(integration['provides'], ['accounts', 'transactions'])
        self.assertTrue({'bank_profile', 'blz'} <= {field['key'] for field in integration['fields']})
        profile_field = next(field for field in integration['fields'] if field['key'] == 'bank_profile')
        self.assertIn('atruvia-1', profile_field['label']['en'])
        self.assertNotIn('product_id', {field['key'] for field in integration['fields']})
        self.assertTrue(re.match(r'^[a-z0-9-]+/[a-z0-9-]+$', manifest['id']))


if __name__ == '__main__':
    unittest.main()
