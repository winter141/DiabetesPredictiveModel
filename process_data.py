import glob
import os
from abc import ABC, abstractmethod
from enum import Enum
import numpy as np
import pandas
import pandas as pd

MMOL_TO_MGDL = 18
X_LABELS: dict = {
    "static_user": ["UserID", "Sex", "BMI", "Body weight", "Height", "Self-identity"],
    # "dynamic_user": ["HR", "Calories (Activity)", "Mets"],
    "log": ["Energy", "Carbohydrate", "Protein", "Fat", "Fiber"],
    # Engineered Features
    "temporal_cgm": ["cgm_p30", "cgm_p60", "cgm_p120"],
    "temporal_food": ["meal_hour", "time_since_last_meal"]
}
Y_LABEL = "auc"
MIN_CGM_READINGS = 20  # Minimum cgm readings to qualify food for model


class Dataset(Enum):
    OLD = 0
    CG_MACROS = 1


def get_feature_names(x_labels_dict=None):
    if x_labels_dict is None:
        x_labels_dict = X_LABELS
    flat_x_labels = []
    for section in x_labels_dict.values():
        flat_x_labels.extend(section)
    return flat_x_labels


def process_xlsx(filepath) -> dict[str, pandas.DataFrame]:
    """
    :param filepath:
    :return: Dictionary of Food Data/CGM data dataframes
    """
    return pd.read_excel(filepath, sheet_name=None)


def load_dataframe(filepath="./data/log_with_cgm.pkl") -> pandas.DataFrame:
    return pd.read_pickle(filepath)


class DataABC(ABC):

    @abstractmethod
    def pickle(self):
        pass


class OldData(DataABC):
    def __init__(self, dataframes: dict[str, pandas.DataFrame], out: str):
        self.log_df = dataframes["TEI_Cleaned"]
        self.cgm_df = dataframes["CGM_Cleaned"]
        self.out = out

        # Ensure datetime
        self.log_df['Timestamp'] = pd.to_datetime(self.log_df['Timestamp'])
        self.cgm_df['NZT'] = pd.to_datetime(self.cgm_df['NZT'])

    def pickle(self):
        static_columns = ["UserID", "Sex"]
        static_user_df = self.log_df[static_columns].drop_duplicates().reset_index(drop=True)

        log_columns = ["UserID", "Timestamp", "Carbohydrate", "Protein", "Fat", "Energy", "FoodItem", "Tag",  "Weight"]
        log_df = self.log_df[log_columns]

        self.cgm_df.rename(columns={"UserID": "UserID", "NZT": "Timestamp", "value": "reading"}, inplace=True)

        static_user_df.to_pickle(os.path.join(self.out, "static_user.pkl"))
        log_df.to_pickle(os.path.join(self.out, "log.pkl"))
        self.cgm_df.to_pickle(os.path.join(self.out, "cgm.pkl"))


class CGMacrosData(DataABC):

    def __init__(self, log_dfs: list[tuple[pandas.DataFrame, int]], bio_df: pandas.DataFrame, out: str):
        self.log_dfs = log_dfs
        self.bio_df = bio_df
        self.out = out

        for log_df, user_id in self.log_dfs:
            log_df['Timestamp'] = pd.to_datetime(log_df['Timestamp'])
            log_df["UserID"] = user_id
            log_df.rename(columns={'Calories': 'Energy', "Carbs": "Carbohydrate", "Libre GL": "reading"}, inplace=True)
            log_df["reading"] /= MMOL_TO_MGDL
        self.bio_df.rename(columns={'subject': 'UserID', 'Gender': 'Sex'}, inplace=True)
        self.bio_df['Sex'] = self.bio_df['Sex'].map({'M': 1, 'F': 0})

    def pickle(self):
        self.bio_df.rename(columns={'subject': 'UserID'}, inplace=True)

        log_columns = ["UserID", "Timestamp", "Meal Type", "Energy", "Carbohydrate", "Protein", "Fat", "Fiber"]
        dynamic_user_columns = ["UserID", "Timestamp", "HR", "Calories (Activity)", "METs"]
        cgm_columns = ["UserID", "Timestamp", "reading"]
        processed_log_df, processed_dynamic_user_df, processed_cgm_df = [], [], []
        for log_df, _ in self.log_dfs:
            processed_log_df.append(log_df[log_df["Meal Type"].notna()][log_columns])
            processed_dynamic_user_df.append(log_df[dynamic_user_columns])
            processed_cgm_df.append(log_df[cgm_columns])
        full_log_df = pd.concat(processed_log_df, ignore_index=True)
        full_dynamic_user_df = pd.concat(processed_dynamic_user_df, ignore_index=True)
        full_cgm_df = pd.concat(processed_cgm_df, ignore_index=True)

        self.bio_df.to_pickle(os.path.join(self.out, "static_user.pkl"))
        full_log_df.to_pickle(os.path.join(self.out, "log.pkl"))
        full_dynamic_user_df.to_pickle(os.path.join(self.out, "dynamic_user.pkl"))
        full_cgm_df.to_pickle(os.path.join(self.out, "cgm.pkl"))


def pickle_data(dataset: Dataset):

    if dataset == Dataset.OLD:
        dataframes = process_xlsx("data/old/CGM_TEI_Cleaned(1).xlsx")
        return OldData(dataframes, "data/old/pickle/").pickle()

    elif dataset == Dataset.CG_MACROS:
        cgm_folder_path = "data/CGMacros/cgm"
        file_pattern = os.path.join(cgm_folder_path, "*.csv")
        log_dataframes = [(pd.read_csv(file), int(os.path.basename(file).split('-')[1].split('.')[0])) for file in glob.glob(file_pattern)]
        bio_dataframe = pd.read_csv("data/CGMacros/bio.csv")
        return CGMacrosData(log_dataframes, bio_dataframe, "data/CGMacros/pickle/").pickle()

    raise ValueError("Dataset selected is not valid")


class FeatureLabelReducer:

    def __init__(self, df_dict: dict[str, pandas.DataFrame]):
        self.static_user_df = df_dict["static_user"]
        self.log_df = df_dict["log"]
        self.cgm_df = df_dict["cgm"]
        self.dynamic_user_df = None if "dynamic_user" not in df_dict else df_dict["dynamic_user"]

        self.cgm_df['Timestamp'] = pd.to_datetime(self.cgm_df['Timestamp'])
        self.cgm_df = self.cgm_df.sort_values('Timestamp')

        self.full_df = None

    def reduce_cgm_window_to_area(self, row, hours):
        """
        Compute iAUC by summing only positive areas above baseline.
        """
        time_window = pd.Timedelta(hours=hours)
        reduced_cgm = self.cgm_df[
            (self.cgm_df["UserID"] == row["UserID"]) &
            (self.cgm_df["Timestamp"] >= row["Timestamp"]) &
            (self.cgm_df["Timestamp"] <= row["Timestamp"] + time_window)
            ]

        if len(reduced_cgm) < MIN_CGM_READINGS:
            return np.nan

        baseline = reduced_cgm.iloc[0]["reading"]

        delta_minutes = reduced_cgm['Timestamp'].diff().dt.total_seconds() / 60
        rolling_mean = reduced_cgm['reading'].rolling(2).mean() - baseline
        incremental_auc = (rolling_mean * delta_minutes).fillna(0)

        iAUC = incremental_auc[incremental_auc > 0].sum()
        return iAUC

    def join_all(self):
        self.full_df = self.static_user_df.merge(self.log_df, on="UserID", how="left")

        self.full_df["auc"] = self.full_df.apply(lambda row: self.reduce_cgm_window_to_area(row, 2), axis=1)

        # Add temporal (engineered) features
        self.full_df["meal_hour"] = self.full_df['Timestamp'].dt.hour
        for label, previous_hours in [("cgm_p30", 0.5), ("cgm_p60", 1), ("cgm_p120", 2)]:
            self.full_df[label] = (
                self.full_df.apply(
                    lambda row: self._cgm_reading(row, previous_hours),
                    axis=1)
            )

        def apply_cgm_current(row):
            matching = self.cgm_df[
                (self.cgm_df["Timestamp"] == row["Timestamp"]) &
                (self.cgm_df["UserID"] == row["UserID"])
            ].copy()
            if not matching.empty:
                return matching["reading"].iloc[0]
            return None

        self.full_df["cgm_current"] = self.full_df.apply(apply_cgm_current, axis=1)

        self.full_df["time_since_last_meal"] = self.full_df.apply(self._time_since_last_meal, axis=1)

        # Make user a categorical feature
        self.full_df["UserID"] = self.full_df["UserID"].astype(str)

        return self.full_df

    def _time_since_last_meal(self, row):
        timestamp = row["Timestamp"]
        user = row["UserID"]

        # Log dataframe is in order, so we can make use of that...
        earlier_logs = self.log_df[
            (user == self.log_df["UserID"]) &
            (self.log_df["Timestamp"] < timestamp)
            ]

        if earlier_logs.empty:
            return None

        last_meal_time = earlier_logs["Timestamp"].max()
        time_diff = (timestamp - last_meal_time).total_seconds() / 60
        return time_diff

    def _cgm_reading(self, row, previous_hours, max_tolerance_minutes=10):
        timestamp = row["Timestamp"]
        user = row["UserID"]

        target_time = timestamp - pd.Timedelta(hours=previous_hours)

        user_cgm = self.cgm_df[
            (self.cgm_df["UserID"] == user) &
            (self.cgm_df["Timestamp"] < timestamp)
            ].copy()

        if user_cgm.empty:
            return None

        user_cgm["time_diff"] = (user_cgm["Timestamp"] - target_time).abs()
        closest_row = user_cgm.loc[user_cgm["time_diff"].idxmin()]

        # Check if it's within the tolerance
        if closest_row["time_diff"].total_seconds() / 60 <= max_tolerance_minutes:
            return closest_row["reading"]
        return None

    def get_x_y_data(self, x_labels_dict=None, y_label=Y_LABEL, users: list = None):
        if x_labels_dict is None:
            x_labels_dict = X_LABELS

        reduced: pandas.DataFrame = self.join_all()
        reduced = reduced[reduced[y_label].notna()]  # drop rows where auc is nan

        if users is not None:
            reduced = reduced[reduced["UserID"].isin(users)]

        feature_names = get_feature_names(x_labels_dict)

        x_df = reduced[feature_names]
        x_df = pd.get_dummies(x_df)

        x_values = x_df.values
        feature_names = x_df.columns

        y_values = reduced[y_label].values

        return feature_names, x_values, y_values


if __name__ == "__main__":
    # pickle_data(Dataset.CG_MACROS)
    base_file_path = "data/CGMacros/pickle/"
    df_dict = dict()
    for pkl in ["cgm", "dynamic_user", "log", "static_user"]:
        df_dict[pkl] = load_dataframe(base_file_path + pkl + ".pkl")
    # ----------------------------------- #
    reducer = FeatureLabelReducer(df_dict)
    feature_names, x, y = reducer.get_x_y_data()
    print(len(x))

    np.save("data/CGMacros/feature_label/feature_names.npy", feature_names)
    np.save("data/CGMacros/feature_label/x.npy", x)
    np.save("data/CGMacros/feature_label/y.npy", y)
