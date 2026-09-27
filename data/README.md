# Data

Raw Dominick's files are not distributed in this repository. Obtain the cereal-category UPC and movement files from the [Kilts Center for Marketing](https://www.chicagobooth.edu/research/kilts/research-data/dominicks) under the provider's terms and place them locally as:

```text
data/raw/upccer.csv
data/raw/wcer.csv
data/raw/wcer.zip
```

The variable definitions and source conventions are documented in the official [Dominick's manual and codebook](https://www.chicagobooth.edu/-/media/enterprise/centers/kilts/datasets/dominicks-dataset/dominicks-manual-and-codebook_kiltscenter).

Run the notebooks or corresponding scripts from the repository root. Generated analytical outputs are written under `data/processed/`, which is intentionally excluded from version control. The small aggregate CSVs required by the public Tableau workbook are retained under `tableau/data/`; they do not contain raw scanner rows.

This independent analysis does not imply endorsement by the Kilts Center for Marketing or the University of Chicago Booth School of Business.
