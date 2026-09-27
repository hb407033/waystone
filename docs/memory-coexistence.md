# Coexistence rules: Waystone and native memory

Native memory keeps personal experience, repo files maintain official rules, and the shared library maintains provenance-tracked team knowledge. No automatic two-way sync; agents resolve conflicts per the skill.

New environment, branch, and source_version fields are added; old records keep their original text and topic with the new fields empty. Only one active record per topic/environment/branch; deduplication considers the canonical topic, scope, source version, and content. New topics are normalized (NFKC, lowercase, whitespace trimmed around slashes, collapsed internal whitespace); old topics match by canonical name but are not rewritten; automatic selection is refused when multiple valid aliases exist.

Queries first pass project ACL and SQL status checks, then distinguish matched records, unspecified-scope records, and pending proposals in the same scope. Queries without a scope return a scope warning. This is not semantic contradiction detection, and it doesn't guarantee a single winning fact. Environment and branch define scope, not access isolation; member permissions are still governed by the project ACL.

Retrieval first selects the valid scope from SQL, then runs vector search in batches by candidate ID with the correct top_k, merging results by relevance. New entries/page and reindex return cursors; clients must follow next_cursor until empty. Full rebuilds are owner-only and backfill missing vectors. Old proposals can be rebased onto the current active version after owner review, or rejected; both keep an audit trail. Re-saving an expired handoff creates a new version.

The database upgrade adds three columns and replaces the old topic index with a scope index, keeping old content and IDs. Pause this service before deploying and take a consistent snapshot with the SQLite backup API. Rollback must restore both the old image and the pre-upgrade database — never just the image; post-upgrade writes must be exported and preserved before rolling back, never discarded outright.
