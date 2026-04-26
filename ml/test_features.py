import pandas as pd
import numpy as np
from features import build_dataset, describe_dataset, get_targets

# Données synthétiques — 60 jours
dates = pd.date_range("2024-01-01", periods=60)

daily = pd.DataFrame({
    "date": dates,
    "user": "gauthier",
    "hrv_last_night": np.random.normal(45, 8, 60),
    "resting_hr": np.random.normal(55, 5, 60),
    "sleep_duration_min": np.random.normal(420, 40, 60),
    "sleep_score": np.random.normal(70, 10, 60),
})

activities = pd.DataFrame({
    "date": dates[::2],  # séance un jour sur deux
    "user": "gauthier",
    "activity_type": "running",
    "avg_hr": np.random.normal(145, 10, 30),
    "max_hr": np.random.normal(170, 8, 30),
    "avg_speed_kmh": np.random.normal(11, 1, 30),
    "duration_min": np.random.normal(50, 10, 30),
    "elevation_gain_m": np.random.normal(100, 50, 30),
    "training_effect": np.random.uniform(2, 4, 30),
})

daily.to_csv("data/daily_metrics.csv", index=False)
activities.to_csv("data/activities.csv", index=False)

X, meta, stats = build_dataset(
    "data/daily_metrics.csv",
    "data/activities.csv",
    user="gauthier",
    save_dir="data/processed/",
)

describe_dataset(X, meta)
X_ctx, X_tgt = get_targets(X, horizon=1)
print(f"Paires JEPA : X_ctx={X_ctx.shape}, X_tgt={X_tgt.shape}")