# German FinTS Banks for Securo

A read-only bank provider plugin for [Securo](https://github.com/securo-finance/securo). It uses `python-fints` to read accounts, balances, and transactions from a fixed list of German bank/provider profiles. It does not initiate payments or transfers.

**This plugin is AI-generated and was developed with AI assistance.** It is an independent community project and is not affiliated with any bank or Securo. ING has been tested with one live connection; other bank profiles have not been broadly production-verified.

## Requirements

- Securo **0.20.0 or later**, with the code plugin runner and egress proxy enabled. See the [Securo bank provider guide](https://github.com/securo-finance/securo/blob/v0.20.0/docs/plugins/bank-provider-plugins.md).
- A German bank account that supports FinTS PIN/TAN, a registered FinTS product ID, online banking user ID, and PIN/password.
- A supported bank/provider profile and its eight-digit BLZ, if that profile does not provide a default.

## Bank profiles

Enter one of these profile keys in **Bank profile**:

| Key | Bank/provider group | BLZ |
| --- | --- | --- |
| `atruvia` | Atruvia (Volksbanken/Raiffeisenbanken, Sparda, PSD) | Required |
| `gls` | GLS Bank (Atruvia endpoint) | Fixed at `43060967` |
| `ing` | ING Germany | Fixed at `50010517` |
| `gad` | Legacy GAD endpoint | Required |
| `comdirect` | comdirect | Required |
| `deutsche-bank` | Deutsche Bank | Required |
| `dkb` | DKB | Required |

Securo 0.20's manifest field schema has no select/enum control, so **Bank profile** is a validated text field rather than a dropdown. The plugin maps each accepted profile to a fixed HTTPS endpoint; users cannot enter arbitrary URLs. The corresponding hosts are explicitly declared in `securo-plugin.json`. A listed provider endpoint does not guarantee that every bank or account using it supports the calls this plugin makes; verify the profile and BLZ with your bank.

## Install and configure

1. Install the plugin from the catalog used by your Securo instance.
2. Create a connection for one bank. Enter the profile key, BLZ if required, FinTS user ID, PIN/password, and registered product ID.
3. Optionally enter one or more complete account IBANs, separated by commas, to select only those accounts. Leave the field empty to sync every eligible account the bank reports.
4. Start a sync and compare balances and transactions with your bank before relying on the result.

Use a separate connection for a different bank. Multiple accounts returned by the same FinTS login are handled by that connection. Account selection is validated against the accounts returned by the bank; a missing selected account stops the sync.

For existing ING connections, a blank **Bank profile** continues to use the ING profile and a blank BLZ uses `50010517`. The GLS and ING profiles use their fixed BLZs; supplying a different BLZ for either is rejected.

## Supported data and settings

- Accounts and posted transactions from standard `python-fints` calls: account listing, balance retrieval, and transaction retrieval.
- Account types identified as investment/depot accounts are skipped. Holdings, depot statements, and bank-specific pending balance/transaction extensions are not implemented.
- **Transaction history days:** Optional; defaults to 85 and accepts 1–89 days. Securo's requested sync start date further limits the window.
- **Account IBANs to sync:** Optional, comma-separated full IBANs. This field is stored as a plugin secret.

The plugin does not complete interactive TAN challenges. A first sync or a later strong customer authentication check may require approval in the bank's app; when FinTS returns a TAN/retry challenge, the sync pauses and needs attention.

## Privacy and limitations

- Login, PIN/password, selected IBANs, and serialized FinTS client state are stored as Securo plugin secrets. The plugin does not log bank responses or credentials.
- Network access is restricted to the exact HTTPS hosts listed in the manifest. Wildcards and IP addresses are not used.
- FinTS features vary by bank. Unsupported account types or bank-specific behavior may prevent a sync; verify the imported data against the bank.
- Avoid syncing the same account through multiple providers without reconciling duplicates. Do not remove and recreate a connection unnecessarily: its stored ID salt keeps external account and transaction IDs stable.

## License

The plugin's own code is licensed under [MIT](LICENSE). The `fints` dependency is separately licensed under LGPL-3.0-or-later; its dependencies have their own licenses.
