import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor

from pulso_transmi.pipeline import EXTRA_TREES_FEATURES, EXTRA_TREES_LAGS

frame = pd.read_csv("data/observations.csv")
frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
frame["station_id"] = frame["station_id"].astype(str)
frame = frame.sort_values(["station_id", "observed_at"])
stations = sorted(frame["station_id"].unique())
frame["station_code"] = frame["station_id"].map({s: i for i, s in enumerate(stations)})
local = frame["observed_at"].dt.tz_convert("America/Bogota")
frame["local_dow"] = local.dt.dayofweek
frame["local_slot"] = local.dt.hour * 4 + local.dt.minute // 15
frame["slot_sin"] = np.sin(2 * np.pi * frame["local_slot"] / 96)
frame["slot_cos"] = np.cos(2 * np.pi * frame["local_slot"] / 96)
frame["dow_sin"] = np.sin(2 * np.pi * frame["local_dow"] / 7)
frame["dow_cos"] = np.cos(2 * np.pi * frame["local_dow"] / 7)
frame["is_weekend"] = (frame["local_dow"] >= 5).astype(int)
grouped = frame.groupby("station_id", observed=True)["demand"]
for lag in EXTRA_TREES_LAGS:
    frame[f"lag_{lag}"] = grouped.shift(lag)
daily = [f"lag_{lag}" for lag in EXTRA_TREES_LAGS[:6]]
frame["daily_median"] = frame[daily].median(axis=1)
frame["daily_mean"] = frame[daily].mean(axis=1)
frame["daily_trend"] = frame["lag_96"] - frame["lag_192"]
frame["weekly_trend"] = frame["lag_672"] - frame["lag_1344"]
frame["adaptive_profile_hl14"] = frame.groupby(
    ["station_id", "local_slot"], observed=True
)["demand"].transform(
    lambda values: values.shift(1).ewm(halflife=14, adjust=True).mean()
)
frame["adaptive_weekly_profile_hl14"] = frame.groupby(
    ["station_id", "local_dow", "local_slot"], observed=True
)["demand"].transform(
    lambda values: values.shift(1).ewm(halflife=2, adjust=True).mean()
)
frame = frame.dropna(subset=[*EXTRA_TREES_FEATURES, "demand"])
cutoff = frame["observed_at"].max() - pd.Timedelta(days=7)
train = frame[frame["observed_at"] <= cutoff]
valid = frame[frame["observed_at"] > cutoff].copy()
features = list(EXTRA_TREES_FEATURES)

extra = ExtraTreesRegressor(n_estimators=240, min_samples_leaf=8, max_features=0.8, n_jobs=-1, random_state=42)
extra.fit(train[features], train["demand"])
valid["extra"] = extra.predict(valid[features])

profile = train.groupby(["station_id", "local_dow", "local_slot"], observed=True)["demand"].median()
station_profile = train.groupby("station_id", observed=True)["demand"].median()
global_profile = float(train["demand"].median())
valid["profile"] = [profile.get((row.station_id, row.local_dow, row.local_slot), station_profile.get(row.station_id, global_profile)) for row in valid.itertuples()]

def score(values):
    scored = valid.assign(pred=np.clip(values, 0, None))
    by_station = scored.groupby("station_id", observed=True).apply(lambda g: 100 * max(0, 1 - np.abs(g["demand"] - g["pred"]).sum() / g["demand"].sum()), include_groups=False)
    return float(by_station.mean()), float(np.abs(scored["demand"] - scored["pred"]).mean()), float(by_station.min())

print("rows", len(train), len(valid), "cutoff", cutoff.isoformat())
print("extra", score(valid["extra"]))
print("adaptive_profile_hl14", score(valid["adaptive_profile_hl14"]))
print("adaptive_weekly_profile_hl14", score(valid["adaptive_weekly_profile_hl14"]))
best = (score(valid["extra"])[0], "extra", score(valid["extra"]))
for leaves, leaf_size, regularization in ((15, 10, 0.0), (31, 10, 0.0), (31, 20, 1.0), (63, 10, 1.0), (63, 20, 0.0), (127, 10, 1.0)):
    hgb = HistGradientBoostingRegressor(learning_rate=0.05, max_iter=400, max_leaf_nodes=leaves, min_samples_leaf=leaf_size, l2_regularization=regularization, random_state=42)
    hgb.fit(train[features], train["demand"])
    hgb_values = hgb.predict(valid[features])
    print("hgb", leaves, leaf_size, regularization, score(hgb_values))
    for weight in (0.1, 0.2, 0.3, 0.4):
        candidate = (1-weight)*hgb_values + weight*valid["profile"]
        print("hgb_profile", leaves, leaf_size, regularization, weight, score(candidate))
        for extra_weight in (0.25, 0.5, 0.75):
            stack_score = score(extra_weight*valid["extra"] + (1-extra_weight)*candidate)
            print("stack", leaves, leaf_size, regularization, weight, extra_weight, stack_score)
            if stack_score[0] > best[0]:
                best = (stack_score[0], (leaves, leaf_size, regularization, weight, extra_weight), stack_score)
print("best", best)
