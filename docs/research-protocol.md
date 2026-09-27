# Research Protocol

This project follows a reproducible empirical workflow for studying retail price inflation and price rigidity in Dominick's cereal scanner data.

## Evidence chain

Every substantive claim should remain traceable through:

`research question → hypothesis → data → cleaning → method → result → robustness → interpretation → limitation`

Claims with a missing link are treated as preliminary.

## Core standards

- Report null, mixed, and contrary findings without steering the analysis toward a preferred result.
- Define exclusions before inspecting outcomes where possible, and report sensitivity to consequential choices.
- Follow the official Dominick's documentation and record unresolved field ambiguities.
- Treat `data/raw/` as immutable; place derived outputs under `data/processed/`.
- Document material methodological choices, rationale, alternatives, and expected consequences in the relevant stage-specific methodology file.
- Distinguish observed facts, statistical estimates, economic interpretation, and speculation.
- Check joins, units, missingness, duplicate keys, impossible values, coverage, and selection effects before modelling.
- Use deterministic code, relative project paths, recorded dependencies, and seeded randomness where relevant.
- Preserve prior results or document revisions instead of silently replacing them.

## Review standard

Before treating a result as final, check for denominator errors, price/unit mistakes, data leakage, fragile specifications, overclaiming, and inconsistent definitions across notebooks, tables, figures, and dashboards. Present limitations beside conclusions.

## Project conventions

- The notebook order follows the empirical sequence: audit, cleaning, indices, rigidity and promotions, econometrics.
- Reusable transformations belong in `src/`.
- Bulk derived data should not be committed unless it is needed for validation or review.
- Reporting must distinguish posted price, effective unit price, promotions, and quantities whenever relevant.
