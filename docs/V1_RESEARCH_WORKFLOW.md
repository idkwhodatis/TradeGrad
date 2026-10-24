# V1 strategy-understanding workflow

This increment adds recorded decision evidence and human research notes to the
existing application. It does not turn a rule diagram into executable code,
certify a trading strategy, or infer missing reasons with a model.

## Seven-step review

1. Open a strategy's research workbench at
   `/strategies/{strategyVersionId}/research`. Use **Version lineage** to choose
   a code parent or child, then select its **Actual leaf Evaluation**. A seed
   version is not automatically the code executed by an optimized candidate.
2. Read the rule cards and expected template graph. Select a rule to inspect
   its exact code hash and source lines. The graph is read-only and is explicitly
   separate from the observed execution. Arbitrary Python does not receive an
   invented controlled graph.
3. Open decision replay and choose an explicit event, instrument and stage.
   Inspect recorded features, predicate states, candidate eligibility/rank,
   constraints and both order intent and actual execution links. Selection does
   not establish a purchase: follow saved child-order and fill IDs. Non-trades,
   rejected or unfilled orders and missing observations remain visible.
4. Choose **Compare with code parent**, or explicitly select both immutable
   sides. Read configuration differences and the paired source event, including
   **both decision pre-states**. Different paths can change cash and holdings;
   their differences are not proof that the highlighted rule alone caused an
   outcome. An unchanged section refers only to its named observed evidence.
5. In **Verification**, write the hypothesis and planned mechanism change,
   inspect the fixed protocol, and manually create a two- or three-arm controlled
   experiment. The original baseline is included. Inspect each arm's actual
   saved behavior, not merely its successful status. Failures remain failed,
   retries retain historical generations, and family holdout exposure carries
   forward. Diagnostic replay reconstructs from the original start and warmup;
   narrowing the observation window does not reset the portfolio.
6. Save an evidence note with the mechanism, observations, controlled results,
   counterexamples, applicability and limitations. Status is a human research
   assessment, never an automatic profitability verdict. A revision preserves
   exact source/specification, Evaluation/Attempt, decision, comparison and arm
   references. Updating a note creates a new revision rather than replacing it.
7. Reload or sign in again and open the saved revision. Its restore links recover
   the same immutable selections, not the latest run or source. Search and
   Markdown export obey the same resource and outcome-access rules. Missing,
   unavailable and corrupt references are distinguished, not silently repaired
   by substituting newer evidence.

## Recorded time and trust

Corrected native daily CSV timestamps are **market-local midnight session
labels**, not actual exchange-close or data-availability instants. Replay,
feature cutoffs, charts, comparison pre-states and result exports disclose this.
Historical native timestamps retain their original recorded coordinates with
unverified timing semantics. They are not retroactively relabelled. Portable
engine instants retain the separate portable execution contract.

Same-session OHLC and native same-process observations do not independently
prove intraday information availability or absence of look-ahead. A missing
feature is not zero; a false predicate is not an unevaluated predicate; an
absent pre-state is not an empty portfolio. Trace capture copies already computed
values and must not reevaluate a strategy or change the candidate universe.

## Frozen demonstration and capacity boundaries

The continuous browser acceptance uses the explicitly synthetic
`controlled-cross.synthetic.v1` four-symbol A/B/C/D profile, available only in
fixture mode within `[2024-01-02, 2024-03-01)`. Its hand-worked eight-session
prices repeat over 41 frozen XNYS sessions; its imported universe is user-fixed,
not a claim of historical market membership. The demonstration uses train
`[2024-01-17, 2024-02-01)`, validation `[2024-02-01, 2024-02-22)`, and holdout
`[2024-02-22, 2024-03-01)`, with three prior warmup sessions. It inspects training
evidence without opening the final holdout. B's observed selection is checked
against explicit order/fill links; higher-score C's liquidity rejection is
checked against its saved predicate and the independently executed variant.

This small real-engine demonstration is separate from the 1,000-listing ×
750-session × three-variant synthetic trace-storage/query/comparison capacity
measurement. Neither is live-provider evidence. The portable engine's existing
100-listing contract is unchanged. Full PostgreSQL, isolation, recovery,
Playwright and Compose acceptance must pass on the exact published source head;
unit tests or Playwright collection alone are not that release gate.

See [the engineering contract and phase evidence](V1_RESEARCH_INCREMENT.md),
[native session correction evidence](NATIVE_SESSION_ALIGNMENT.md), and the
required jobs in `.github/workflows/v1-validation.yml`.
