# Tableau Dashboard

`Cereal_Price_Dynamics.twb` is the final four-dashboard Tableau workbook. It connects only to seven compact aggregate CSV files in `data/` and does not embed the raw Dominick's scanner data or row-level analytical samples.

## Contents

- `Cereal_Price_Dynamics.twb`: Tableau workbook.
- `Screenshots/`: rendered images for the four dashboards shown in the repository README.
- `data/`: the seven aggregate CSV sources used by the workbook.

## Connected sources

- `project_overview.csv`
- `weekly_indices.csv`
- `rigidity_summary.csv`
- `promotion_transitions.csv`
- `price_reversals.csv`
- `econometric_coefficients.csv`
- `hypothesis_summary.csv`

The workbook is a communication layer for previously validated results. Index calculations, fixed-effects estimation, confidence intervals and hypothesis evidence are not recomputed inside Tableau.

Data source: Kilts Center for Marketing, University of Chicago Booth School of Business. This independent analysis does not imply endorsement by the Kilts Center or Chicago Booth.
