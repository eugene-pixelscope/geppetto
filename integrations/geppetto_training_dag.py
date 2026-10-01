"""Dedicated Airflow 3 DAG example. NOT installed by Geppetto.

Install only into a separately approved test environment. Requires the CNCF
Kubernetes provider and read-only template/dataset registries baked into images.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path

from airflow.sdk import dag, task
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from kubernetes.client import models as k8s


def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


@dag(dag_id='geppetto_training_v1',schedule=None,start_date=datetime(2026,1,1,tzinfo=timezone.utc),
     catchup=False,max_active_runs=1,tags=['geppetto','isolated-validation'])
def training():
    @task(retries=0)
    def validate(dag_run=None):
        conf=dag_run.conf
        if not re.fullmatch('[a-f0-9]{32}',conf.get('submission_id','')):
            raise ValueError('Invalid submission ID')
        frozen=conf['frozen']
        approved=json.loads(Path(os.environ['GEPPETTO_TEMPLATE_ALLOWLIST']).read_text())
        contract=approved[frozen['template']['id']]
        if sha(frozen['template'])!=contract['template_sha256']:
            raise ValueError('Template differs from the approved image contract')
        image=contract['image']
        if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}',image):
            raise ValueError('Immutable image digest required')
        if frozen['gpu']!={'mode':'auto','count':1}:
            raise ValueError('This DAG has not validated UUID selection or multi-GPU')
        if frozen['relation']!='train':
            raise ValueError('This image supports new training only')
        if sha(frozen['config'])!=frozen['config_sha256']:
            raise ValueError('Config digest mismatch')
        if len(frozen['datasets'])!=1:
            raise ValueError('One composite dataset version required')
        dataset_id=frozen['datasets'][0]['id']
        dataset=contract['datasets'][dataset_id]
        if dataset['version']!=frozen['datasets'][0]['version']:
            raise ValueError('Dataset version is not approved')
        return {'image':image,'dataset_pvc':dataset['pvc'],'dataset_subpath':dataset.get('subpath',''),
                'spec':conf,'submission_id':conf['submission_id']}

    checked=validate()
    submission="{{ ti.xcom_pull(task_ids='validate')['submission_id'] }}"
    # Use a NEW dedicated results PVC and namespace; never the legacy ball-results PVC.
    result_volume=k8s.V1Volume(name='results',persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(claim_name='geppetto-results'))
    data_volume=k8s.V1Volume(name='data',persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
        claim_name="{{ ti.xcom_pull(task_ids='validate')['dataset_pvc'] }}",read_only=True))
    train=KubernetesPodOperator(
        task_id='train',name='geppetto-training',namespace='geppetto-validation',
        image="{{ ti.xcom_pull(task_ids='validate')['image'] }}",
        cmds=['python','/opt/geppetto/integrations/container_entrypoint.py'],
        env_vars={'GEPPETTO_SPEC_JSON':"{{ ti.xcom_pull(task_ids='validate')['spec'] | tojson }}",
                  'AIRFLOW_DAG_ID':'{{ dag.dag_id }}','AIRFLOW_RUN_ID':'{{ run_id }}',
                  'MLFLOW_TRACKING_URI':os.environ.get('GEPPETTO_MLFLOW_URI','http://mlflow:5000')},
        labels={'geppetto-submission':submission},
        volumes=[result_volume,data_volume,k8s.V1Volume(name='cache',empty_dir=k8s.V1EmptyDirVolumeSource())],
        init_containers=[k8s.V1Container(name='prepare-results',image=os.environ.get('GEPPETTO_INIT_IMAGE','REPLACE_WITH_PINNED_IMAGE'),
            command=['sh','-c','mkdir -p "/shared/$SUBMISSION"'],env=[k8s.V1EnvVar(name='SUBMISSION',value_from=k8s.V1EnvVarSource(
                field_ref=k8s.V1ObjectFieldSelector(field_path="metadata.labels['geppetto-submission']")))],
            volume_mounts=[k8s.V1VolumeMount(name='results',mount_path='/shared')])],
        volume_mounts=[k8s.V1VolumeMount(name='results',mount_path='/outputs',sub_path=submission),
                       k8s.V1VolumeMount(name='data',mount_path='/datasets/input',read_only=True,
                                       sub_path="{{ ti.xcom_pull(task_ids='validate')['dataset_subpath'] }}"),
                       k8s.V1VolumeMount(name='cache',mount_path='/cache')],
        container_resources=k8s.V1ResourceRequirements(requests={'cpu':'2','memory':'8Gi','nvidia.com/gpu':'1'},
                                                       limits={'cpu':'8','memory':'32Gi','nvidia.com/gpu':'1'}),
        get_logs=True,on_finish_action='delete_pod',retries=0,do_xcom_push=False,
    )
    checked >> train


training()
