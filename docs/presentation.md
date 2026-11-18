# VibeCheck 2.0: Presentation Outline (5–7 minutes)

Target length: about 6 minutes. Times are cumulative. Each slide lists what to show and what to say.
The **key terms** from the course are in bold where they fit naturally.

---

## 1. Hook and problem (0:00–0:40)

**Show:** title slide: *VibeCheck 2.0: an agentic music recommender that checks its own work.*

**Say:**
- "If you tell a music app 'nothing sad' and it plays a sad song anyway, it gives you no sign that
  it got it wrong."
- "My goal was a recommender where the AI's output is checked, explained, and flagged when it can't
  deliver, instead of trusted blindly."

## 2. Where it started: VibeCheck 1.0 (0:40–1:15)

**Show:** one line of a 1.0 profile and a scored song with its reasons.

**Say:**
- "In Module 3 I built VibeCheck 1.0: it scores 20 songs against a hand-written taste profile and
  explains every point."
- "Its weakness was that you had to describe your taste in its exact labels, and it failed silently, like a
  k-pop fan getting confident-looking results with zero matches."

## 3. Architecture (1:15–2:30)

**Show:** the diagram from `diagrams/architecture.mmd` (it renders in the README on GitHub).

**Say:**
- "2.0 adds an **agent** that runs a plan, act, check, revise loop. This is the **orchestration**
  layer."
- "**Plan:** Claude (or an offline rule-based planner) turns plain English into a profile and rules,
  like 'avoid sad'."
- "**Act:** it calls my unchanged 1.0 engine as a tool. The AI steers the search, but only the engine picks
  songs, so it can't invent one."
- "**Check and revise:** code verifies every pick, removes songs that break the rules, and tries again."
- "**Guardrails** on both sides: input validation before, **verification** after."
- "**Human-in-the-loop:** runs it can't fully satisfy are flagged for review."
- On **RAG**: "I didn't need retrieval. The catalog is a small structured CSV, so the agent calls
  the engine directly instead of searching documents."

## 4. Live demo (2:30–4:15)

Run from the project root with the virtual environment active. Use `--offline` so the demo
doesn't depend on Wi-Fi or an API key.

1. **The loop fixing itself:**
   ```bash
   python -m src.main --offline --steps "chill music for studying, nothing sad"
   ```
   Point at the steps: round 1 had a melancholic song, round 2 a sad one, both excluded. Final list is
   clean, confidence 0.82.

2. **A request the catalog can't satisfy:**
   ```bash
   python -m src.main --offline "k-pop music for dancing"
   ```
   Point at: "'k-pop' is not a genre in the catalog" and the review flag. "1.0 would have hidden this."

3. **A guardrail:**
   ```bash
   python -m src.main --offline ""
   ```
   Rejected before any AI runs.

**Backup if the demo fails:** open `evidence/01_end_to_end_runs.txt` and `evidence/02_guardrails.txt`,
which hold the same runs saved as text.

## 5. Reliability: how I know it works (4:15–5:15)

**Show:** the evaluation summary line and the before/after table from the README.

```bash
python -m src.evaluate
```

**Say:**
- "**Reliability testing** has three layers: 25 unit tests, a 13-case evaluation set of normal,
  adversarial and malformed requests, and a confidence score on every answer."
- "The evaluation went from 11 of 13 to 13 of 13. It caught two bugs my unit tests missed: 'anything
  but country' recommended country, and 'don't want anything too intense' read *intense* as a wish."
- "Surprise: confidence was lowest on some *correct* answers. Asking for r&b scores 0.20 because
  there's only one r&b song. Confidence ended up measuring catalog depth, not correctness."

## 6. Limits and responsibility (5:15–5:50)

**Say:**
- "It still fails in ways I've documented: 'not a fan of country' puts country at #1 without a flag,
  and non-English or slang requests get generic picks."
- "Claude's free-text summary isn't filtered yet. That's my top fix for misuse."
- Full details are in `model_card.md`, sections 10–13.

## 7. What I learned (5:50–6:30)

**Say:**
- "Give the AI a narrow job, and make everything around it checkable."
- "Measure before claiming: running realistic requests found the bugs that my own tests missed."
- "Working with an AI coding agent, my most useful contribution was keeping the scope tight and asking
  for evidence. For example, I scoped its suggestion to combine two extensions down to one."

**Close:** "VibeCheck doesn't just recommend songs; it shows its work and says when it isn't sure."

---

### Checklist before presenting
- [ ] `source .venv/bin/activate` and run each demo command once beforehand
- [ ] README open on GitHub so the diagram renders
- [ ] `evidence/` files open in a tab as a backup
- [ ] Practice once with a timer and aim for about 6:00
