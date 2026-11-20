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

    def deconstruct(self):
        return b'not-an-actual-bank-blob'


FakeTAN = type('NeedTANResponse', (), {})
TEST_TODAY = date(2026, 11, 18)


def context(saved=None, cfg=None):
    return SimpleNamespace(
        secrets={'login': 'test-login', 'pin': '12345', **(saved or {})},
        config={'product_id': 'registered-test-id', **(cfg or {})},
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
        self.assertEqual(observed['server'], p.BANK_PROFILES['atruvia']['server'])

    def test_bank_code_must_be_eight_digits(self):
        with self.assertRaisesRegex(p.BankSyncError, 'exactly 8 digits'):
            p._sync({}, context(cfg={
                'bank_profile': 'atruvia', 'blz': '4306096x'}), factory=Bank)

    def test_gls_profile_supplies_blz_and_uses_atruvia_host(self):
        observed = {}

        def factory(**kwargs):
            observed.update(kwargs)
            return Bank(**kwargs)

        p._sync({}, context(cfg={'bank_profile': 'gls'}), factory=factory, today=TEST_TODAY)
        self.assertEqual(observed['bank_identifier'], '43060967')
        self.assertEqual(observed['server'], p.BANK_PROFILES['atruvia']['server'])
        with self.assertRaisesRegex(p.BankSyncError, 'must be 43060967'):
            p._sync({}, context(cfg={'bank_profile': 'gls', 'blz': '43060968'}), factory=Bank)

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
        self.assertTrue(re.match(r'^[a-z0-9-]+/[a-z0-9-]+$', manifest['id']))


if __name__ == '__main__':
    unittest.main()
