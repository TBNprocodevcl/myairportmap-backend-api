import boto3

ACCOUNT_ID = "0576111027d795aa466452df95bedd02"
ACCESS_KEY = "c26eb1bccb2ed234727105fa510542f2"
SECRET_KEY = "630ec0a698cdfd8634fafd13215e02f6c7683d7472047af4481ea23b152d081a"
BUCKET_NAME = "mapusers"

s3 = boto3.client(
    service_name='s3',
    endpoint_url=f'https://{ACCOUNT_ID}.r2.cloudflarestorage.com',
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name='auto'
)

# list file
response = s3.list_objects_v2(Bucket=BUCKET_NAME)

for obj in response.get("Contents", []):
    print(obj["Key"])