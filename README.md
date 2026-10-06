# BI Supermarket Site Selection 🛒🗺️

## Overview

This repository contains a geospatial analysis and location modeling project developed for the *Data Analysis and Machine Learning* course.

The main objective is to identify optimal strategic locations for new supermarkets in two major Chilean metropolitan areas:

- **Greater Santiago**
- **Greater Concepción**

By applying Spatial ETL workflows, the project integrates demographic information and urban infrastructure data to identify areas with high demand potential and optimal accessibility.

---

## Key Features & Methodology

- **Spatial Data Extraction:** Reading and filtering large geospatial datasets, including OpenStreetMap road networks and 2024 Census block-level data.
- **Data Cleaning & Transformation:** Processing geometries, handling missing values, and generating relevant spatial features such as transport accessibility and demographic density.
- **CSV Export for Machine Learning:** Structuring and exporting processed spatial data into ready-to-use `.csv` datasets for future predictive modeling and site selection algorithms.

---

## Tech Stack 🛠️

- **Language:** Python
- **Environment:** Jupyter Notebooks
- **Geospatial Libraries:** `geopandas`, `osmnx`, `shapely`
- **Data Manipulation:** `pandas`, `numpy`

---

## Data Download & Setup 📥

Due to their size, the geospatial and demographic datasets are not included in this repository.

To reproduce the analysis locally:

1. Create the following directory:

```text
data/raw/
```

2. Download the required datasets:

   - **2024 Census Data:** Available from the official INE website:
     https://censo2024.ine.gob.cl/

   - **OpenStreetMap Data (Chile):** Available from Geofabrik:
     https://download.geofabrik.de/south-america/chile.html

3. Install the required dependencies:

```bash
pip install -r requirements.txt
```

4. Run the notebook:

```text
01_spatial_data_processing.ipynb
```

---

## Repository Structure

```text
.
├── data/
│   ├── raw/                     # Ignored in .gitignore (input datasets)
│   └── processed/               # Ignored in .gitignore (generated CSV datasets)
├── 01_spatial_data_processing.ipynb
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Authors

- Mateo JR
- Elizabeth Echeverría