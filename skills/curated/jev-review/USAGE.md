# Portable JEV package

Copy the entire `jev-review` folder to a workspace or the agent's skill directory. Keep its subdirectories intact. Python 3.10 or later is the only local dependency. An authorized TypeSafe API key and outbound HTTPS are required for live calls.

## Copy and test

From the folder containing `jev-review`, choose one destination:

```sh
# Hermes; use its actual profile home if it differs.
mkdir -p "$HOME/.hermes/skills"
cp -R jev-review "$HOME/.hermes/skills/"
cd "$HOME/.hermes/skills/jev-review"
python3 scripts/jev.py --self-test
python3 scripts/jev.py --input examples/evidence.json --dry-run
```

For Codex, use `~/.codex/skills/jev-review` instead. For another agent, keep the folder in its workspace and provide `SKILL.md` as its tool instructions. Do not overwrite an existing installation without inspecting it. Skill discovery depends on the host agent; this package does not modify or restart that agent.

With `TYPESAFE_API_KEY` already injected into the process environment by your existing secret manager:

```sh
python3 scripts/jev.py --input examples/evidence.json
```

No key belongs in the copied folder. No Mini, Spark, SSH alias, or shared account is required. Each destination uses its own authorized credential provision. The supplied example makes one live request; offline self-test and dry-run make none.

On Windows PowerShell:

```powershell
Copy-Item -Recurse .\jev-review <your-agent-skill-directory>
Set-Location <your-agent-skill-directory>\jev-review
py -3 scripts/jev.py --self-test
py -3 scripts/jev.py --input examples/evidence.json --dry-run
# After the approved environment already supplies TYPESAFE_API_KEY:
py -3 scripts/jev.py --input examples/evidence.json
```

## Request

Supply JSON with `state` (nonempty text, object, or array) and `questions`. Each named question has `type: choice`, nonempty `instructions`, and a map of label names to nonempty descriptions. This package permits 1–16 questions, 2–32 labels per question, and at most 80,000 encoded request bytes. These are package limits, not provider capacity claims. Scores and yes/no probability primitives are outside this version.

The fixed provider is `https://api.typesafe.ai/v1/systemone`, with model `jev-1.13.0`. No arbitrary endpoint or model override is accepted. Calls have a 20-second network timeout and no automatic retries. Redirects are rejected. Do not substitute a chat-completions endpoint.

Input can come from a file or stdin:

```sh
python3 scripts/jev.py --input request.json > result.json
python3 scripts/jev.py < request.json > result.json
```

## Result

The JSON result includes schema, status, `advisory_only: true`, `review_required: true`, source and request hashes, and validated answers when available. Hashes bind content and questions; they are not proof that the underlying source is authentic. Usage is returned only when valid token counts were supplied by the provider.

Exit status 0 means a reviewed result or successful dry-run. Exit 2 means invalid input. Exit 3 means unavailable credentials, transport, or provider output. Check both status and exit code. A successful dry-run does not mean JEV was called. Provider failures do not expose raw response bodies or exception text.

## Integrate with a workflow

1. Use the existing workflow to select and sanitize evidence.
2. Construct the explicit questions without reference labels or expected answers.
3. Call the helper once and parse its JSON.
4. Attach the advisory result and hashes to the existing review step.
5. Keep the existing action checks and owner decision in control.

For Enoch, start with inbox leftovers and commitment evidence. For Doc, start with incident and completion claims. For Testing Bot, run offline failure cases and then a permitted live sample. This portable package does not itself bind those runtimes, distribute credentials, schedule calls, or authorize rollout.
