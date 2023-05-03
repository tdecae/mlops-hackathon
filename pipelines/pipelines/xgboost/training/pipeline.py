# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import pathlib

from kfp.v2 import compiler, dsl
from kfp.v2.components import importer_node
from pipelines import generate_query
from pipelines.components import (
    extract_bq_to_dataset,
    bq_query_to_table,
    update_best_model,
    import_model_evaluation,
    custom_train_job,
)


@dsl.pipeline(name="xgboost-train-pipeline")
def xgboost_pipeline(
    project_id: str = os.environ.get("VERTEX_PROJECT_ID"),
    project_location: str = os.environ.get("VERTEX_LOCATION"),
    ingestion_project_id: str = os.environ.get("VERTEX_PROJECT_ID"),
    model_name: str = "simple_xgboost",
    dataset_id: str = "preprocessing",
    dataset_location: str = os.environ.get("VERTEX_LOCATION"),
    ingestion_dataset_id: str = "chicago_taxi_trips",
    timestamp: str = "2022-12-01 00:00:00",
):
    """
    XGB training pipeline which:
     1. Splits and extracts a dataset from BQ to GCS
     2. Trains a model via Vertex AI CustomTrainingJob
     3. Evaluates the model against the current champion model
     4. If better the model becomes the new default model

    Args:
        project_id (str): project id of the Google Cloud project
        project_location (str): location of the Google Cloud project
        ingestion_project_id (str): project id containing the source bigquery data
            for ingestion. This can be the same as `project_id` if the source data is
            in the same project where the ML pipeline is executed.
        model_name (str): name of model
        dataset_id (str): id of BQ dataset used to store all staging data & predictions
        dataset_location (str): location of dataset
        ingestion_dataset_id (str): dataset id of ingestion data
        timestamp (str): Optional. Empty or a specific timestamp in ISO 8601 format
            (YYYY-MM-DDThh:mm:ss.sss±hh:mm or YYYY-MM-DDThh:mm:ss).
            If any time part is missing, it will be regarded as zero.
    """

    # Create variables to ensure the same arguments are passed
    # into different components of the pipeline
    label_column_name = "total_fare"
    time_column = "trip_start_timestamp"
    ingestion_table = "taxi_trips"
    table_suffix = "_xgb_training"  # suffix to table names
    ingested_table = "ingested_data" + table_suffix
    preprocessed_table = "preprocessed_data" + table_suffix
    train_table = "train_data" + table_suffix
    valid_table = "valid_data" + table_suffix
    test_table = "test_data" + table_suffix
    primary_metric = "rootMeanSquaredError"
    test_dataset_uri = None

    # generate sql queries which are used in ingestion and preprocessing
    # operations

    queries_folder = pathlib.Path(__file__).parent / "queries"

    ingest_query = generate_query(
        queries_folder / "ingest.sql",
        source_dataset=f"{ingestion_project_id}.{ingestion_dataset_id}",
        source_table=ingestion_table,
        filter_column=time_column,
        target_column=label_column_name,
        filter_start_value=timestamp,
    )
    split_train_query = generate_query(
        queries_folder / "sample.sql",
        source_dataset=dataset_id,
        source_table=ingested_table,
        num_lots=10,
        lots=tuple(range(8)),
    )
    split_valid_query = generate_query(
        queries_folder / "sample.sql",
        source_dataset=dataset_id,
        source_table=ingested_table,
        num_lots=10,
        lots="(8)",
    )
    data_cleaning_query = generate_query(
        queries_folder / "engineer_features.sql",
        source_dataset=dataset_id,
        source_table=train_table,
    )

    # data ingestion and preprocessing operations

    kwargs = dict(
        bq_client_project_id=project_id,
        destination_project_id=project_id,
        dataset_id=dataset_id,
        dataset_location=dataset_location,
        query_job_config=json.dumps(dict(write_disposition="WRITE_TRUNCATE")),
    )
    ingest = bq_query_to_table(
        query=ingest_query, table_id=ingested_table, **kwargs
    ).set_display_name("Ingest data")

    # exporting data to GCS from BQ

    split_train_data = (
        bq_query_to_table(query=split_train_query, table_id=train_table, **kwargs)
        .after(ingest)
        .set_display_name("Split train data")
    )
    split_valid_data = (
        bq_query_to_table(query=split_valid_query, table_id=valid_table, **kwargs)
        .after(ingest)
        .set_display_name("Split validation data")
    )
    data_cleaning = (
        bq_query_to_table(
            query=data_cleaning_query, table_id=preprocessed_table, **kwargs
        )
        .after(split_train_data)
        .set_display_name("Data Cleansing")
    )

    # data extraction to gcs

    train_dataset = (
        extract_bq_to_dataset(
            bq_client_project_id=project_id,
            source_project_id=project_id,
            dataset_id=dataset_id,
            table_name=preprocessed_table,
            dataset_location=dataset_location,
            file_pattern="",
        )
        .after(data_cleaning)
        .set_display_name("Extract train data to storage")
    ).outputs["dataset"]
    valid_dataset = (
        extract_bq_to_dataset(
            bq_client_project_id=project_id,
            source_project_id=project_id,
            dataset_id=dataset_id,
            table_name=valid_table,
            dataset_location=dataset_location,
            file_pattern="",
        )
        .after(split_valid_data)
        .set_display_name("Extract validation data to storage")
    ).outputs["dataset"]

    if test_dataset_uri:
        test_dataset = (
            importer_node.importer(
                artifact_uri=test_dataset_uri, artifact_class=dsl.Dataset
            )
            .set_display_name("Import test data")
            .outputs["artifact"]
        )
    else:
        split_test_query = generate_query(
            queries_folder / "sample.sql",
            source_dataset=dataset_id,
            source_table=ingested_table,
            num_lots=10,
            lots="(9)",
        )
        split_test_data = (
            bq_query_to_table(query=split_test_query, table_id=test_table, **kwargs)
            .after(ingest)
            .set_display_name("Split test data")
        )
        test_dataset = (
            extract_bq_to_dataset(
                bq_client_project_id=project_id,
                source_project_id=project_id,
                dataset_id=dataset_id,
                table_name=test_table,
                dataset_location=dataset_location,
                file_pattern="",
            )
            .after(split_test_data)
            .set_display_name("Extract test data to storage")
        ).outputs["dataset"]

    hparams = dict(
        n_estimators=200,
        early_stopping_rounds=10,
        objective="reg:squarederror",
        booster="gbtree",
        learning_rate=0.3,
        min_split_loss=0,
        max_depth=6,
        label=label_column_name,
    )

    task = importer_node.importer(
        artifact_uri="gs://dt-turbo-templates-dev-pl-assets/training/assets/task.py",
        artifact_class=dsl.Artifact,
    ).set_display_name("Import task")

    train_model = custom_train_job(
        task=task.output,
        train_data=train_dataset,
        valid_data=valid_dataset,
        test_data=test_dataset,
        project_id=project_id,
        project_location=project_location,
        model_display_name=model_name,
        train_container_uri="europe-docker.pkg.dev/vertex-ai/training/scikit-learn-cpu.0-23:latest",  # noqa: E501
        serving_container_uri="europe-docker.pkg.dev/vertex-ai/prediction/sklearn-cpu.0-24:latest",  # noqa: E501
        hparams=hparams,
        requirements=["scikit-learn==0.24.0"],
    ).set_display_name("Train model")

    evaluation = import_model_evaluation(
        model=train_model.outputs["model"],
        metrics=train_model.outputs["metrics"],
        test_dataset=test_dataset,
        pipeline_job_id="{{$.pipeline_job_name}}",
        project_location=project_location,
    ).set_display_name("Import evaluation")

    with dsl.Condition(train_model.outputs["parent_model"] != "", "champion-exists"):
        update_best_model(
            challenger=train_model.outputs["model"],
            challenger_evaluation=evaluation.outputs["model_evaluation"],
            parent_model=train_model.outputs["parent_model"],
            eval_metric=primary_metric,
            eval_lower_is_better=True,
            project_id=project_id,
            project_location=project_location,
        )


if __name__ == "__main__":
    compiler.Compiler().compile(
        pipeline_func=xgboost_pipeline,
        package_path="training.json",
        type_check=False,
    )
