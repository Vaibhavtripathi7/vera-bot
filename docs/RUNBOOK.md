# Runbook: keys → VM → verify → submit

## 1. Keys (about 5 minutes)

1. **Gemini:** at https://aistudio.google.com/apikey, create an API key (free, no card needed).
2. **Check your real free-tier limits:** open https://aistudio.google.com/rate-limit. Write down the RPM and RPD for `gemini-2.5-flash` and `gemini-2.5-flash-lite`.
3. **Groq (optional fallback):** at https://console.groq.com/keys, create a key.

## 2. VM (Oracle Cloud Always Free, or GCP e2-micro)

- **Oracle:**
  - Create an instance with Ubuntu 24.04, shape `VM.Standard.E2.1.Micro`, or A1 Flex if available. Use the Mumbai region if you can.
  - Add your SSH key.
  - Networking → VCN → Security List → add ingress rules for TCP **80** and **443** from `0.0.0.0/0`.
- **GCP:**
  - Create an e2-micro instance in us-west1, us-central1 or us-east1 (the free-tier regions), with Ubuntu 24.04.
  - Tick "Allow HTTP/HTTPS traffic".

## 3. Deploy (run these from your laptop, in the repo folder)

```bash
VM=ubuntu@<VM_PUBLIC_IP>
rsync -az --exclude .venv --exclude .git --exclude '*.db*' --exclude eval/out --exclude expanded ./ $VM:~/vera-src/
ssh $VM 'sudo bash ~/vera-src/deploy/setup_vm.sh'          # prints your https URL (<ip-with-dashes>.sslip.io)
ssh $VM 'sudo nano /etc/vera.env'                           # paste keys + RPM/RPD from step 1, CONTACT_EMAIL
ssh $VM 'sudo systemctl restart vera'
curl https://<ip-with-dashes>.sslip.io/v1/healthz
curl https://<ip-with-dashes>.sslip.io/v1/debug/llm         # providers present + healthy
```

- **Redeploy after any change:** `bash deploy/push.sh $VM`
- **Logs:** `ssh $VM 'journalctl -u vera -f'`

## 4. Verify against the deployed URL (before submitting)

```bash
export GEMINI_API_KEY=...        # a judge key; ideally a different project from the bot's key
uv run python -m eval.harness --url https://<your-url> --judge gemini     # or --judge groq to save Gemini quota
# read eval/out/summary.json (judge_avg, penalties, latency) and eval/out/review.md
python judge_simulator.py        # official: set BOT_URL, LLM_PROVIDER, LLM_API_KEY at the top of the file first
curl -X POST https://<your-url>/v1/teardown                  # wipe test state afterwards
```

Target before submitting:
- `penalties: []`
- `/tick` p99 under 8 s
- judge average ≥ 8.5 on each dimension

## 5. Submit

- **When:** right after the daily Gemini quota reset (midnight Pacific ≈ 12:30–13:30 IST), with no eval runs afterwards that day.
- **Uptime monitor:** add a free UptimeRobot check on `https://<url>/v1/healthz` every 5 minutes.
- **Portal:** submit the base URL (no `/v1`) on https://magicpin.com/vera/ai-challenge, then keep the VM running.
