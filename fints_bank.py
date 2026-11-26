"""Read-only German FinTS provider for Securo >=0.20.0.

Plugin API: https://github.com/securo-finance/securo/blob/v0.20.0/docs/plugins/bank-provider-plugins.md
No transfers or payments; no HTTP endpoint. Securo runs Plugin.sync itself.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets as pysecrets
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

PRODUCT_ID = 'DCAAD26F9DB177903C2DE1F0A'
LEGACY_BANK_CODE = '50010517'
LEGACY_SERVER = 'https://fints.ing.de/fints/'
DEFAULT_BANK_PROFILE = 'ing'
BANK_PROFILES = {
    'atruvia': {
        'label': 'Atruvia group (fints2)',
        'server': 'https://fints2.atruvia.de/cgi-bin/hbciservlet',
    },
    'atruvia-1': {
        'label': 'Atruvia group (fints1)',
        'server': 'https://fints1.atruvia.de/cgi-bin/hbciservlet',
    },
    'gls': {
        'label': 'GLS Bank',
        'server': 'https://fints1.atruvia.de/cgi-bin/hbciservlet',
        'fixed_blz': '43060967',
        'institution_name': 'GLS Bank',
    },
    'ing': {
        'label': 'ING Germany',
        'server': LEGACY_SERVER,
        'fixed_blz': LEGACY_BANK_CODE,
        'institution_name': 'ING Deutschland',
    },
    'gad': {
        'label': 'Legacy GAD',
        'server': 'https://hbci-pintan.gad.de/cgi-bin/hbciservlet',
    },
    'comdirect': {
        'label': 'comdirect',
        'server': 'https://fints.comdirect.de/fints',
        'institution_name': 'comdirect',
    },
    'deutsche-bank': {
        'label': 'Deutsche Bank',
        'server': 'https://fints.deutsche-bank.de/',
        'institution_name': 'Deutsche Bank',
    },
    'dkb': {
        'label': 'DKB',
        'server': 'https://fints.dkb.de/fints',
        'institution_name': 'DKB',
    },
}
ALLOWED_FINTS_HOSTS = frozenset({
    'fints1.atruvia.de',
    'fints2.atruvia.de',
    'fints.ing.de',
    'hbci-pintan.gad.de',
    'fints.comdirect.de',
    'fints.deutsche-bank.de',
    'fints.dkb.de',
})
CONTRACT = 1
MAX_TX = 20_000
TAN_WAIT_SECONDS = 35
TAN_POLL_INTERVAL = 5
TAN_MAX_POLLS = 6


class NeedsAuth(Exception):
    """Securo should pause polling until credentials / approval are refreshed."""


class BankSyncError(Exception):
    """Bank or data-format failure with a non-sensitive message."""


def _text(value, size=500) -> str:
    if isinstance(value, (list, tuple)):
        value = ' '.join(_text(item, size) for item in value if item is not None)
    elif isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    return ' '.join(str(value or '').split())[:size]


def _money(value) -> Decimal:
    if hasattr(value, 'amount'):
        value = value.amount
    try:
        v = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BankSyncError('FinTS bank returned an unreadable amount; nothing was imported') from None
    if not v.is_finite():
        raise BankSyncError('FinTS bank returned an invalid amount; nothing was imported')
    return v


def _as_iso_day(value) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            pass
    raise BankSyncError('FinTS booking has no valid posting date; nothing was imported')


def _digest(salt: str, prefix: str, *values) -> str:
    # HMAC prevents revealing the IBAN and preserves IDs across syncs.
    content = '\x1f'.join(str(value) for value in values).encode('utf-8')
    return prefix + hmac.new(bytes.fromhex(salt), content, hashlib.sha256).hexdigest()[:40]


def _transaction_rows(items, salt: str, account_id: str) -> list[dict]:
    """Normalize mt940 and fints.models.Transaction rows to Securo contract 1.

    Truly identical rows without a bank transaction ID can only be identified by
    their occurrence number; reordering identical bank records is impossible to
    disambiguate without an actual bank-assigned immutable ID.
    """
    prepared = []
    for item in items:
        raw = getattr(item, 'data', None)
        if not isinstance(raw, dict):
            raise BankSyncError('FinTS bank returned an unknown transaction format; nothing was imported')
        date = _as_iso_day(raw.get('entry_date') or raw.get('booking_date') or raw.get('bookingDate') or raw.get('date'))
        amount = _money(raw.get('amount'))
        desc = _text(raw.get('purpose') or raw.get('transaction_details') or
                     raw.get('remittance_information') or raw.get('applicant_name') or
                     raw.get('extra_details') or 'FinTS transaction', 500)
        payee = _text(raw.get('applicant_name') or raw.get('recipient_name') or raw.get('counterparty_name'), 200)
        references = [_text(raw.get(k), 100) for k in (
            'transaction_id', 'bank_reference', 'end_to_end_reference', 'customer_reference', 'id')]
        references = [r for r in references if r.upper() not in ('', 'NOTPROVIDED', 'NONREF', 'NONE')]
        # The fingerprint includes the business fields even when a reference is present.
        fingerprint = (account_id, date, str(amount), desc, payee, *references)
        prepared.append((fingerprint, {
            'description': desc,
            'amount': format(abs(amount), 'f'),
            'date': date,
            'type': 'credit' if amount >= 0 else 'debit',
            'currency': _text(getattr(raw.get('amount'), 'currency', None) or raw.get('currency') or 'EUR', 3).upper(),
            'status': 'posted',
            **({'payee': payee} if payee else {}),
        }))
    occurrence = Counter()
    rows = []
    # Sorting by content makes duplicate occurrence IDs independent of API order.
    for fingerprint, row in sorted(prepared, key=lambda pair: pair[0]):
        n = occurrence[fingerprint]
        occurrence[fingerprint] += 1
        row['external_id'] = _digest(salt, 'tx-', *fingerprint, n)
        rows.append(row)
    if len(rows) > MAX_TX:
        raise BankSyncError('Too many FinTS transactions for one Securo synchronization; shorten history window')
    return rows


def _account_type(product: str) -> str:
    p = product.casefold()
    if any(word in p for word in ('extra-konto', 'extrakonto', 'tagesgeld', 'spar', 'savings')):
        return 'savings'
    if any(word in p for word in ('depot', 'investment', 'wertpapier')):
        return 'investment'
    return 'checking'


def _get_bank_info(bank) -> dict:
    value = bank.get_information() or {}
    return {str(a.get('iban', '')).replace(' ', '').upper(): a
            for a in value.get('accounts', []) if isinstance(a, dict) and a.get('iban')}


def _require_result(bank, value, *, stage: str, deadline: float):
    from fints.client import NeedTANResponse

    def reject_failed_approval(_segment, response):
        if response.code.startswith('9'):
            raise NeedsAuth('The bank rejected app approval; no data was imported')

    if isinstance(value, NeedTANResponse):
        if not value.decoupled:
            raise NeedsAuth('The bank requires a TAN entry; this plugin only supports app approval')
        mechanism = bank.get_tan_mechanisms().get(bank.get_current_tan_mechanism())
        if getattr(mechanism, 'automated_polling_allowed', None) is False:
            raise NeedsAuth('The bank does not allow automatic approval polling')
        max_polls = getattr(mechanism, 'decoupled_max_poll_number', None)
        if max_polls is None:
            max_polls = TAN_MAX_POLLS
        if not isinstance(max_polls, int) or max_polls < 1:
            raise BankSyncError('Invalid bank approval polling limit; nothing was imported')
        max_polls = min(max_polls, TAN_MAX_POLLS)
        first_wait = getattr(mechanism, 'wait_before_first_poll', None)
        next_wait = getattr(mechanism, 'wait_before_next_poll', None)
        for wait in (first_wait, next_wait):
            if wait is not None and (not isinstance(wait, int) or wait < 0):
                raise BankSyncError('Invalid bank approval polling interval; nothing was imported')
        first_wait = max(TAN_POLL_INTERVAL, first_wait or 0)
        next_wait = max(TAN_POLL_INTERVAL, next_wait or 0)
        for poll in range(max_polls):
            wait = first_wait if poll == 0 else next_wait
            if time.monotonic() + wait >= deadline:
                break
            time.sleep(wait)
            if time.monotonic() >= deadline:
                break
            bank.add_response_callback(reject_failed_approval)
            try:
                value = bank.send_tan(value, '')
            finally:
                bank.remove_response_callback(reject_failed_approval)
            if not isinstance(value, NeedTANResponse):
                break
            if not value.decoupled:
                raise NeedsAuth('The bank requires a TAN entry; this plugin only supports app approval')
        if isinstance(value, NeedTANResponse):
            raise NeedsAuth('App approval was not completed in time; start a new sync and approve the prompt promptly')
    if value is None:
        raise BankSyncError(f'FinTS bank returned no {stage}; nothing was imported')
    if value.__class__.__name__ == 'NeedRetryResponse':
        raise NeedsAuth('The bank requires an unsupported FinTS verification step')
    return value


def _settings(params, context):
    config = context.config or {}
    secret = context.secrets or {}
    login = _text(secret.get('login'))
    pin = str(secret.get('pin') or '')
    product_id = PRODUCT_ID.strip()
    bank_profile = str(config.get('bank_profile') or DEFAULT_BANK_PROFILE).strip().casefold()
    profile = BANK_PROFILES.get(bank_profile)
    if profile is None:
        choices = ', '.join(BANK_PROFILES)
        raise BankSyncError(f'Unknown bank profile; choose one of: {choices}')
    entered_blz = str(config.get('blz') or '').strip()
    fixed_blz = profile.get('fixed_blz')
    if fixed_blz and entered_blz and entered_blz != fixed_blz:
        raise BankSyncError(f'BLZ for {profile["label"]} must be {fixed_blz}')
    bank_identifier = entered_blz or fixed_blz or ''
    server = profile['server']
    if not login or not pin:
        raise NeedsAuth('FinTS user ID and PIN are required')
    if not product_id:
        raise BankSyncError('Plugin product ID is not configured; set PRODUCT_ID in fints_bank.py')
    if not bank_identifier:
        raise BankSyncError(f'BLZ is required for {profile["label"]}')
    if not bank_identifier.isascii() or not bank_identifier.isdigit() or len(bank_identifier) != 8:
        raise BankSyncError('Bank code (BLZ) must contain exactly 8 digits')
    days = 85
    if config.get('history_days'):
        try:
            days = int(config['history_days'])
        except (ValueError, TypeError):
            raise BankSyncError('History days must be a whole number from 1 to 89') from None
    if not 1 <= days <= 89:
        raise BankSyncError('History days must be between 1 and 89')
    salt = secret.get('id_salt') or pysecrets.token_hex(32)
    if len(salt) != 64:
        raise BankSyncError('Saved ID salt is invalid; do not reset it or account IDs will change')
    try:
        bytes.fromhex(salt)
    except ValueError:
        raise BankSyncError('Saved ID salt is invalid') from None
    return login, pin, product_id, bank_profile, profile, bank_identifier, server, days, salt


def _sync(params, context, *, factory=None, today=None) -> dict:
    tan_deadline = time.monotonic() + TAN_WAIT_SECONDS
    login, pin, product_id, bank_profile, profile, bank_identifier, server, days, salt = _settings(params, context)
    config = context.config or {}
    saved_secrets = context.secrets or {}
    today = today or dt.datetime.now(ZoneInfo('Europe/Berlin')).date()
    start = today - dt.timedelta(days=days)
    if params.get('since'):
        try:
            requested = dt.date.fromisoformat(params['since'])
        except ValueError:
            raise BankSyncError('Invalid Securo sync window') from None
        start = max(start, requested)
    if start > today:
        start = today
    from fints.client import FinTS3PinTanClient, NeedTANResponse
    if factory is None:
        factory = FinTS3PinTanClient
    legacy_ing_connection = (bank_profile == 'ing' and bank_identifier == LEGACY_BANK_CODE
                             and server == LEGACY_SERVER)
    identity_content = (login + '\x00' + product_id if legacy_ing_connection else
                        '\x00'.join((login, product_id, bank_identifier, server)))
    identity = hashlib.sha256(identity_content.encode()).hexdigest()
    restored = None
    if saved_secrets.get('fints_identity') == identity and saved_secrets.get('fints_client_blob'):
        try:
            restored = base64.b64decode(saved_secrets['fints_client_blob'], validate=True)
        except (ValueError, TypeError):
            raise BankSyncError('Invalid saved FinTS client state') from None
    kwargs = {'bank_identifier': bank_identifier, 'user_id': login, 'pin': pin,
              'server': server, 'product_id': product_id}
    if restored:
        kwargs['from_data'] = restored
    try:
        client = factory(**kwargs)
        with client:
            bank = client
            if isinstance(getattr(bank, 'init_tan_response', None), NeedTANResponse):
                _require_result(bank, bank.init_tan_response, stage='dialog initialization', deadline=tan_deadline)
                bank.init_tan_response = None
            listed = _require_result(bank, bank.get_sepa_accounts(), stage='account list', deadline=tan_deadline)
            if not listed:
                raise BankSyncError('FinTS bank returned no SEPA accounts; existing accounts are unchanged')
            selection = str(saved_secrets.get('account_ibans') or '').strip()
            if selection:
                selected = {''.join(part.split()).upper() for part in selection.replace(';', ',').split(',')}
                if any(not iban or not iban.isascii() or not iban.isalnum() or not 15 <= len(iban) <= 34 for iban in selected):
                    raise BankSyncError('Invalid account selection; enter complete IBANs separated by commas')
                available = {str(getattr(acc, 'iban', '') or '').replace(' ', '').upper() for acc in listed}
                if not selected <= available:
                    raise BankSyncError('At least one selected IBAN was not returned by the bank; nothing was imported')
                listed = [acc for acc in listed if str(getattr(acc, 'iban', '') or '').replace(' ', '').upper() in selected]
            metadata = _get_bank_info(bank)
            accounts = []
            for acc in listed:
                iban = _text(getattr(acc, 'iban', '')).replace(' ', '').upper()
                if not iban:
                    raise BankSyncError('A bank account has no stable IBAN; nothing was imported')
                acc_id = (_digest(salt, 'ing-', iban) if legacy_ing_connection else
                          _digest(salt, 'fints-', bank_identifier, iban))
                product = _text(metadata.get(iban, {}).get('product_name') or 'FinTS account', 180)
                if _account_type(product) == 'investment':
                    continue
                currency = _text(metadata.get(iban, {}).get('currency') or 'EUR', 3).upper()
                if len(currency) != 3 or not currency.isalpha():
                    raise BankSyncError('Unexpected account currency; nothing was imported')
                balance_obj = _require_result(bank, bank.get_balance(acc), stage='account balance', deadline=tan_deadline)
                bal = _money(getattr(balance_obj, 'amount', balance_obj))
                tx_objs = _require_result(bank, bank.get_transactions(acc, start_date=start, end_date=today),
                                          stage='transactions', deadline=tan_deadline)
                rows = _transaction_rows(tx_objs, salt, acc_id)
                accounts.append({
                    'external_id': acc_id,
                    'stable_external_id': acc_id,
                    'name': product,
                    'type': _account_type(product),
                    'balance': format(bal, 'f'),
                    'currency': currency,
                    'institution_name': profile.get('institution_name') or f'FinTS bank (BLZ {bank_identifier})',
                    'institution_external_id': 'ing-de' if legacy_ing_connection else f'fints-{bank_identifier}',
                    'masked_number': iban[-4:],
                    'transactions': rows,
                })
            if len(accounts) > 500 or sum(len(a['transactions']) for a in accounts) > MAX_TX:
                raise BankSyncError('FinTS result exceeds Securo batch limits; reduce history_days')
        # Avoid persisting PIN in log files: serialized client state goes to encrypted Securo plugin vault.
        try:
            client_data = base64.b64encode(client.deconstruct()).decode('ascii')
        except Exception:
            client_data = None
    except (NeedsAuth, BankSyncError):
        raise
    except Exception as exc:
        # Never expose bank server messages: they may include IBANs, TAN data or secrets.
        cls = type(exc).__name__
        if cls == 'FinTSClientPINError':
            raise NeedsAuth('The bank rejected FinTS dialog initialization (FinTSClientPINError); the cause is not necessarily an incorrect PIN') from None
        if cls == 'FinTSClientTemporaryAuthError':
            raise NeedsAuth('The bank reports that FinTS access is temporarily locked (3938); contact the bank before retrying') from None
        if cls == 'FinTSSCARequiredError':
            raise NeedsAuth('The bank requires strong customer authentication (9075); this plugin cannot complete an interactive approval') from None
        if cls in ('FinTSConnectionError', 'FinTSNoResponseError', 'TimeoutError',
                   'ConnectTimeout', 'ReadTimeout', 'ConnectionError'):
            raise BankSyncError(f'FinTS connection failed ({cls}); no data was imported') from None
        tb = exc.__traceback__
        while tb and tb.tb_next:
            tb = tb.tb_next
        location = f'{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}' if tb else 'unknown'
        name = getattr(exc, 'name', None) if isinstance(exc, AttributeError) else None
        detail = f', attribute {name}' if isinstance(name, str) and name.isidentifier() and len(name) <= 80 else ''
        raise BankSyncError(f'FinTS sync failed ({cls}, {location}{detail}); no data was changed') from None

    bank_secrets = {'id_salt': salt, 'fints_identity': identity}
    if client_data:
        bank_secrets['fints_client_blob'] = client_data
    return {
        'state': {'last_read_day': today.isoformat()},
        'secrets': bank_secrets,
        'data': {'contract': CONTRACT, 'accounts': accounts},
    }


class Plugin:
    def sync(self, params: dict, context) -> dict:
        return _sync(params or {}, context)


plugin = Plugin()
