# Dataset-owned answer prompts

Scenario runs load `answering` from the benchmark skill manifest:

```yaml
answering:
  profile: longmemeval_answer@1
  prompt_template: prompts/answer_v1.txt
  required_fields: [question, question_date]
```

Both backend_direct and agent_plugin receive the same answer rules. Evidence is
provided by the direct recall step or the Agent's memory plugin respectively;
this does not imply identical full Agent context. Existing skills without an
answer profile use `generic_memory_qa@1` (brief, evidence-grounded answers).
Native case_builder workflows are unchanged.

Templates support `{question}`, `{question_date}` and `{options}`. Question date
and options come from question metadata. Missing required values or unknown
placeholders fail during plan construction, before memory or model calls.
Do not include reference answers or judge rubrics in answer templates.

LoCoMo v2 preserves the evidence-supported inference rules and adds the question date.
LongMemEval v1 includes the question date, temporal updates and explicit abstention.
Ingest content and backend extraction prompts are unchanged.

Each scenario run saves `prompts/answer.txt`, the configured judge template,
and `prompt_manifest.json` with profiles, SHA-256 digests and Git state.
The composed plan and Agent artifacts preserve rendered requests, including
per-question dates and recalled evidence. Increment the profile version when
changing behavior; content digests also detect edits without a version bump.

Judge model and generation settings are recorded from the configured runtime.
The answer model remains Agent-owned: set `MEMORY_BENCH_ANSWER_MODEL` to record
an operator-declared model label (this does not select or override the model).
When absent it is recorded as unknown, not guessed. Preserve a sanitized runtime
configuration alongside results when comparing systems; verify actual model
identity against Agent response artifacts. Never archive API keys.

For comparisons, fix dataset revision, answer profile, model and generation
settings, evaluation rules and evidence budget. Treat backend extraction tuning
as an explicit experimental condition. A successful run does not imply official
upstream prompt parity or improvement on held-out data.


## LoCoMo time baseline (v2)

LoCoMo answer profile `locomo_answer@2` and judge profile `locomo_qa@2`
use `question_date` from the last nonempty conversation session in session-number
order. This is the platform's evaluation convention, aligned with locomo-test.
The date and `question_date_source=last_nonempty_session` are preserved in
question metadata and the composed case reference. Missing/invalid dates fail
during scenario construction; the machine's current date is never substituted.
Both backend_direct and agent_plugin receive the same baseline. Historical
relative dates use the corresponding conversation date, while elapsed-time
answers use the question date. Gold answers and ingestion timestamps are unchanged.
LongMemEval continues to use its dataset-provided question date. Previous LoCoMo prompt versions are available in Git history and saved run
snapshots; only the current v2 templates are kept in the skill directory.
v1/v2 scores are not identical experimental conditions.
