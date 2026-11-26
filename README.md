# German FinTS Banks for Securo

A read-only bank provider plugin for [Securo](https://github.com/securo-finance/securo). It uses `python-fints` to read accounts, balances, and transactions from a fixed list of German bank/provider profiles. It does not initiate payments or transfers.

**This plugin is AI-generated and was developed with AI assistance.** It is an independent community project and is not affiliated with any bank or Securo. ING has been tested with one live connection; other bank profiles have not been broadly production-verified.

## Requirements

- Securo **0.20.0 or later**, with the code plugin runner and egress proxy enabled. See the [Securo bank provider guide](https://github.com/securo-finance/securo/blob/v0.20.0/docs/plugins/bank-provider-plugins.md).
- A German bank account that supports FinTS PIN/TAN, an online banking user ID, and PIN/password.
- A supported bank/provider profile and its eight-digit BLZ, if that profile does not provide a default.

## Bank profiles

Enter one of these profile keys in **Bank profile**:

| Key | Bank/provider group | BLZ |
| --- | --- | --- |
| `atruvia` | Atruvia group, `fints2.atruvia.de` | Required |
| `atruvia-1` | Atruvia group, `fints1.atruvia.de` | Required |
| `gls` | GLS Bank (Atruvia endpoint) | Fixed at `43060967` |
| `ing` | ING Germany | Fixed at `50010517` |
| `gad` | Legacy GAD endpoint (deprecated; availability unverified) | Required |
| `comdirect` | comdirect | Required |
| `deutsche-bank` | Deutsche Bank | Required |
| `dkb` | DKB | Required (usually `12030000`) |

Securo 0.20's manifest field schema has no select/enum control, so **Bank profile** is a validated text field rather than a dropdown. The plugin maps each accepted profile to a fixed HTTPS endpoint; users cannot enter arbitrary URLs. The corresponding hosts are explicitly declared in `securo-plugin.json`. A listed provider endpoint does not guarantee that every bank or account using it supports the calls this plugin makes; verify the profile and BLZ with your bank.

## Install and configure

1. Install the plugin from the catalog used by your Securo instance.
2. Create a connection for one bank. Enter the profile key, BLZ if required, FinTS user ID, and PIN/password.
3. Optionally enter one or more complete account IBANs, separated by commas, to select only those accounts. Leave the field empty to sync every eligible account the bank reports.
4. Start a sync and compare balances and transactions with your bank before relying on the result.

Use a separate connection for a different bank. Multiple accounts returned by the same FinTS login are handled by that connection. Account selection is validated against the accounts returned by the bank; a missing selected account stops the sync.

For existing ING connections, a blank **Bank profile** continues to use the ING profile and a blank BLZ uses `50010517`. The GLS and ING profiles use their fixed BLZs; supplying a different BLZ for either is rejected.

The GLS profile uses `https://fints1.atruvia.de/cgi-bin/hbciservlet`. For other Atruvia banks, select `atruvia-1` or `atruvia` according to the host specified by your bank; neither host works for every institution. For GLS PIN/TAN, use your VR-NetKey or online banking alias without special characters as the FinTS user ID, and your online banking PIN. FinTS access and the TAN method must be activated with the bank, with a valid TAN device assigned. See the [FinTS setup guide](https://vrkennung.de/fintshbci/).

For ING, use your online banking username and password. ING requires an online banking login with the second factor at least every 90 days; see [ING's FinTS help](https://www.ing.de/hilfe/log-in/fints/). For comdirect and Deutsche Bank, enter the BLZ of your account or branch; comdirect uses its access number as the login. The DKB and Deutsche Bank endpoints follow third-party FinTS directories, not a live authenticated test; confirm them with your bank if needed. The legacy `gad` hostname is deprecated; use a current Atruvia profile when your bank specifies one.

Endpoint references consulted in November 2026 include [ING's FinTS help](https://www.ing.de/hilfe/log-in/fints/), the GLS setup shared by the user, and the [GiroCrab FinTS presets](https://github.com/GiroCrab/GiroCrab/blob/2f11a540c0b806735414ca4a0b9405d803fd1356/girocrab-core/src/connectors/fints_presets.rs). Sources may change, and an endpoint's presence does not establish that this plugin works with every account or TAN method. When a profile's endpoint changes, the plugin discards its saved FinTS client state and may require renewed bank authorization; its account IDs are preserved.

Before packaging or publishing, the plugin maintainer must set `PRODUCT_ID` in `fints_bank.py` to this product's registered FinTS number. Sync fails explicitly until it is set; individual connections do not need to enter it.

## Supported data and settings

- Accounts and posted transactions from standard `python-fints` calls: account listing, balance retrieval, and transaction retrieval.
- Account types identified as investment/depot accounts are skipped. Holdings, depot statements, and bank-specific pending balance/transaction extensions are not implemented.
- **Transaction history days:** Optional; defaults to 85 and accepts 1–89 days. Securo's requested sync start date further limits the window.
- **Account IBANs to sync:** Optional, comma-separated full IBANs. This field is stored as a plugin secret.

The plugin supports FinTS decoupled app approvals, such as a SecureGo plus push, when offered by the bank. Start a sync and approve the prompt in the banking app promptly. The plugin polls only during that sync, respecting the bank's polling limits, and waits at most 35 seconds from the start of the sync; Securo does not show a separate verification screen. If approval takes too long, the sync pauses without importing data and a new sync is needed. Scheduled syncs can also require approval.

TANs that must be typed, other interactive challenge types, banks that prohibit automatic polling, and selecting a particular TAN device or method are not supported. An authentication rejection is not necessarily an approval request. App approval has not been verified against a live GLS connection.

## Privacy and limitations

- Login, PIN/password, selected IBANs, and serialized FinTS client state are stored as Securo plugin secrets. The plugin does not log bank responses or credentials.
- Network access is restricted to the exact HTTPS hosts listed in the manifest. Wildcards and IP addresses are not used.
- FinTS features vary by bank. Unsupported account types or bank-specific behavior may prevent a sync; verify the imported data against the bank.
- Avoid syncing the same account through multiple providers without reconciling duplicates. Do not remove and recreate a connection unnecessarily: its stored ID salt keeps external account and transaction IDs stable.

## License

The plugin's own code is licensed under [MIT](LICENSE). The `fints` dependency is separately licensed under LGPL-3.0-or-later; its dependencies have their own licenses.
