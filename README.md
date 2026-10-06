# BI Supermarket Site Selection 🛒🗺️

## Overview
This repository contains the geospatial analysis and location modeling project developed for the Data Analysis and Machine Learning course. The main objective is to evaluate and determine the optimal strategic locations for new supermarkets in two major Chilean urban areas: **Greater Santiago** and **Greater Concepción**.

By applying Spatial ETL workflows, this project integrates demographic data and urban infrastructure layers to identify areas with high demand potential and optimal accessibility.

## Key Features & Methodology
- **Spatial Data Extraction:** Reading and filtering large geospatial datasets (including OpenStreetMap networks and 2024 Census block-level data).
- **Data Cleaning & Transformation:** Processing geometries, handling missing values, and generating relevant spatial features (e.g., transport accessibility, demographic density).
- **CSV Export for ML:** Structuring and exporting the processed spatial data into ready-to-use tabular formats (`.csv`) for future predictive modeling and site selection algorithms.

## Tech Stack 🛠️
- **Language:** Python
- **Environment:** Jupyter Notebooks
- **Geospatial Libraries:** `geopandas`, `osmnx`, `shapely`
- **Data Manipulation:** `pandas`, `numpy`

## Repository Structure
```text
├── notebooks/
│   └── 01_spatial_data_processing.ipynb       # Extracts raw data, performs ETL, and exports to CSV
├── data/
│   ├── raw/                             # (Ignored in .gitignore) Large input geospatial files
│   └── processed/                       # (Ignored in .gitignore) Output .csv files for ML
├── .gitignore
├── requirements.txt
└── README.md
