# trial/repos/

The Credit and Consent Manager trial stacks mount this folder. Put these checkouts here
(they are ignored by git):

| Folder | Source | Base commit | Then |
|---|---|---|---|
| `oan_messaging_connector` | the `oan_messaging_connector` repository | branch `develop` | nothing |
| `oan_a2c_service` | your Credit repository | `3887b8b` | `git am ../../../integrations/patches/oan_a2c_service/*.patch` |
| `oan_auth_service` | your Auth repository | `34c7306` | nothing (A2C requires it) |
| `consent-management` | your DPI repository | `b6d5e45` | `git am ../../../integrations/patches/consent-management/*.patch` |

If your branch has moved past the base commit, apply the patches to the base on a new
branch first, then rebase.
