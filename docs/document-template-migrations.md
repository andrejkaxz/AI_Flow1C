# Template migration and recovery contract

Local configuration schema 0→1→2 is handled by `scripts/migrate-local-config.py`.
Before mutation it writes a unique backup (the requested backup directory or
`.workspace/backups/local-config-migration/`). It preserves unknown user fields,
documentation_path and functional_spec_template; repeated execution on schema 2
does not rewrite the file. Schema >2 is blocked without creating or modifying
configuration/library data. The CLI also rejects future schema versions.

Schema 2 adds `template_library`: `schema_version=1`, `storage_kind=documentation`,
`root=document-templates`, nullable `library_id`. `require_word_for` is an additive
project rule, for example `["functional-spec"]`. The fixed relative library root
does not permit arbitrary configuration paths. Changing documentation_path uses
explicit relocate, not silent merging or a config-only edit.

Library schemas are version 1. Compatible extractor revisions do not automatically
reinterpret saved profiles. An incompatible schema requires a future explicit
migration; this version reports a recovery action instead of rewriting it.
Legacy DOCX import is optional and uses the ordinary intake/profile-save/activate
pipeline. The config migrator never claims semantic analysis or guide readiness.

Recovery journal checkpoints live in `document-templates/.state/operations/`:
RECEIVED/FAILED_RECOVERABLE keeps the copied source; resume retries parsing after
dependency repair. NEEDS_CLARIFICATION keeps profile-draft and questions;
profile-save persists the next answer/profile. PROFILE_COMMITTING completes the
immutable directory commit and index registration without changing active.
Activation uses expected_revision; a concurrent change is a conflict, not an
implicit last-writer-wins update. Pins keep historical revisions.
ACTIVATING records the expected pointer and default decision before changing the
index. Resume accepts the expected or already published pointer; a different
concurrent pointer is preserved and reported as a conflict.

WRITE_PENDING in a saved document plan recovers publication only when the exact
output hash matches the recorded pending result. A different existing file is
preserved and rejected. Missing/corrupt sources, guides, profiles or outputs are
reported by integrity checks; restore a verified backup or create a new revision.
Do not repair immutable files in place. Old library copies survive relocation.

Exclusive locks are not broken automatically. After a crash, verify that no
library writer remains before removing the indicated `.template-library.lock`.
Operation IDs, content hashes and active/default pointers remain recoverable.
Cleanup/deletion, publication and anonymization are separate explicit actions.
