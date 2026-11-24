# Golden renders (SMS must not change)

`helm template oan-msg . -n oan-messaging-dev` output for `values.yaml`, `values-pilot.yaml`
and the `ci/0[1-4]-*-values.yaml` files, taken from `main` before the Telegram worker was
added. `check_unchanged.py` fails if any of them renders differently.

A deliberate change to these renders updates the golden file in the same pull request,
with the reason in the pull request text:

    python3 ci/regression/check_unchanged.py --update
