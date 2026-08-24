# WISER — a short guide for sponsors

WISER answers questions about Connecticut storm outages in plain English. Ask
it something, and it works out what you meant, writes the SQL, runs it against
the storm database, and explains the result.

The data covers **294 storms**, **8 Connecticut counties**, and **2,352**
storm-by-county weather and outage records.

---

## Getting in

Open the link you were sent and sign in with the email address it was sent to.
The app is private, so only invited addresses can reach it.

**If the first load takes a while**, that is expected — the app sleeps after a
long quiet period and takes about half a minute to wake. It stays responsive
after that.

---

## Questions that work well

Every query below has been run against the live database. Type them exactly,
or use them as templates.

**Where does the damage happen**

- `which county was affected the most overall?`
- `top counties by outage`
- `normalize county outages by overhead line exposure`

That last one is worth trying. Raw outage counts mostly track how much
infrastructure a county has; normalizing by overhead line exposure shows which
counties fare worse than their size explains. Fairfield takes about 8,891
outages per million feet of line against Hartford's 3,973 — a gap the raw
totals hide.

**Which storms mattered**

- `top storms by outage`
- `which storm had the highest peak wind?`
- `month-by-month storm outages`
- `compare July storms with February storms`

**Digging into one place or storm**

- `history for county Hartford`
- `for storm 2020080406, why did Hartford have more outages than New Haven despite lower wind?`

The second is the one to try if you only try one. It is not a lookup — the
system reasons about why two counties responded differently to the same storm.

**Finding comparable storms**

- `find storms similar to wind=9.2, precip=45 in Fairfield`

Similarity search needs `key=value` pairs. Asking in prose — "storms like
Fairfield with high wind" — returns an error asking for parameters rather than
guessing at numbers you did not give it.

**Asking how it works**

- `explain the storm similarity algorithm`
- `regional storm analysis`

---

## Things worth knowing

**Follow-up questions work.** Ask `top counties by outage`, then just
`what about Hartford?` — it carries the previous query's meaning forward
rather than starting over.

**It shows its work.** "Show generated SQL" in the sidebar reveals the exact
query that produced any answer, so nothing has to be taken on trust.

**Repeated questions are instant.** The first time a question is asked it goes
to a language model and takes a few seconds. Ask it again and the answer comes
back in under a tenth of a second, from the same live data.

**Some questions honestly return nothing.** `which storms had wind above 40mph?`
gives zero rows, because no storm in this dataset reached that speed — the
maximum is about 13 m/s. An empty result is an answer, not a failure.

**Population data is not in this dataset.** Asking for population-normalized
outages returns exposure-normalized figures instead, and says so, rather than
inventing numbers.

---

## If something goes wrong

The app is designed to explain rather than crash. If a question does not match
anything, it suggests ones that do.

Anything that looks genuinely broken — a blank screen, a repeated error, an
answer that seems wrong — is worth reporting, along with the exact question you
asked. That is useful feedback, not a nuisance.
