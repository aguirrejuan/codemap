from airflow.operators.python import PythonOperator

from features.compute import compute_all


def build_dag():
    return PythonOperator(task_id="compute", python_callable=compute_all)
