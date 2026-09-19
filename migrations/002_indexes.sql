-- Indexes added after measuring the queries without them. Numbers: docs/results.md, Phase 6.

-- Every read filters by recording and orders or ranges by sample, so this serves all of them.
-- It also fixes the ON DELETE CASCADE from recordings, which without an index on the foreign
-- key had to scan the whole annotations table.
CREATE INDEX idx_ann_rec_sample ON annotations (recording_id, sample_index);

-- Abnormal beats are ~17 % of rows and are queried often. The partial index is 0.6 MB and
-- added ~1 % to ingest time, for ~4.5x faster abnormal-beat queries. The planner also uses it
-- for `aami_class = 'V'`, because it can prove that 'V' implies <> 'N', but only when it sees
-- the value: a generic prepared plan falls back to idx_ann_rec_sample (see results.md).
CREATE INDEX idx_ann_abnormal ON annotations (recording_id, sample_index)
  WHERE aami_class <> 'N';

ANALYZE annotations;
