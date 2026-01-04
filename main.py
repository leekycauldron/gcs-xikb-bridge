import os
import json
import requests
import tempfile
import mimetypes
from google.cloud import storage
import functions_framework

# CONFIG & CONSTANTS
ELEVENLABS_API_KEY = os.getenv('ELEVEN_LABS_API_KEY')
ELEVENLABS_AGENT_ID = os.getenv('AGENT_ID')
ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1"

# Initialize Storage Client
storage_client = storage.Client()

def get_elevenlabs_docs():
    """Fetches all documents currently in the ElevenLabs Knowledge Base."""
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    documents = {} 
    
    next_cursor = None
    while True:
        params = {"page_size": 100}
        if next_cursor:
            params["cursor"] = next_cursor
            
        response = requests.get(
            f"{ELEVENLABS_API_URL}/convai/knowledge-base",
            headers=headers,
            params=params
        )
        
        if response.status_code != 200:
            print(f"Error listing docs: {response.text}")
            break
            
        data = response.json()
        for doc in data.get("documents", []):
            documents[doc["name"]] = doc["id"]
            
        if not data.get("has_more"):
            break
        next_cursor = data.get("next_cursor")
        
    return documents

def delete_elevenlabs_doc(doc_id):
    """Deletes a document from ElevenLabs."""
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    response = requests.delete(
        f"{ELEVENLABS_API_URL}/convai/knowledge-base/{doc_id}",
        headers=headers
    )
    if response.status_code == 200 or response.status_code == 204:
        print(f"Deleted doc ID: {doc_id}")
    else:
        print(f"Failed to delete doc {doc_id}: {response.text}")

def upload_file_to_elevenlabs(bucket_name, blob_name):
    """Downloads file from GCS and uploads to ElevenLabs with strict MIME type mapping."""
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    
    # 1. Map extensions to ElevenLabs specific allowed types
    ext_mapping = {
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.pdf': 'application/pdf',
        '.txt': 'text/plain',
        '.epub': 'application/epub+zip',
        '.html': 'text/html',
        '.md': 'text/markdown'
    }
    
    _, ext = os.path.splitext(blob_name.lower())
    content_type = ext_mapping.get(ext)
    
    # Fallback to mimetypes if not in our manual list
    if not content_type:
        content_type, _ = mimetypes.guess_type(blob_name)
        if not content_type:
            content_type = 'text/plain' # Safest fallback for LLM ingestion

    _, temp_local_filename = tempfile.mkstemp()
    
    try:
        blob.download_to_filename(temp_local_filename)
        headers = {"Xi-api-key": ELEVENLABS_API_KEY}
        
        # We send only the filename, not the full GCS path/folder structure if present
        display_name = os.path.basename(blob_name)
        args = {'name': display_name} 
        
        with open(temp_local_filename, 'rb') as f:
            files = {'file': (display_name, f, content_type)}
            response = requests.post(
                f"{ELEVENLABS_API_URL}/convai/knowledge-base/file",
                headers=headers,
                data=args,
                files=files
            )
            
        if response.status_code == 200:
            data = response.json()
            print(f"Uploaded {display_name} as {content_type}, ID: {data['id']}")
            return data['id']
        else:
            print(f"Failed to upload {blob_name}: {response.text}")
            return None
    finally:
        if os.path.exists(temp_local_filename):
            os.remove(temp_local_filename)

def update_agent_knowledge(valid_docs):
    """Updates the agent to use the new list of documents."""
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }

    try:
        get_resp = requests.get(
            f"{ELEVENLABS_API_URL}/convai/agents/{ELEVENLABS_AGENT_ID}",
            headers=headers
        )
        if get_resp.status_code != 200:
            print(f"Warning: Could not fetch agent ({get_resp.status_code}). Proceeding with update.")
    except Exception as e:
         print(f"Exception fetching agent: {e}. Proceeding with update.")

    new_kb_config = []
    for doc in valid_docs:
        new_kb_config.append({
            "type": "file",
            "id": doc['id'],
            "name": doc['name'],
            "usage_mode": "auto"
        })

    patch_data = {
        "conversation_config": {
            "agent": {
                "prompt": {
                    "knowledge_base": new_kb_config
                }
            }
        }
    }

    patch_resp = requests.patch(
        f"{ELEVENLABS_API_URL}/convai/agents/{ELEVENLABS_AGENT_ID}",
        headers=headers,
        json=patch_data
    )

    if patch_resp.status_code == 200:
        print("Agent configuration successfully updated.")
    else:
        print(f"Failed to update agent: {patch_resp.text}")

@functions_framework.cloud_event
def sync_knowledge_base(cloud_event):
    data = cloud_event.data
    bucket_name = data["bucket"]
    triggered_file_name = data.get("name")
    event_type = cloud_event["type"]

    print(f"Sync started for bucket: {bucket_name}, trigger: {triggered_file_name}")

    bucket = storage_client.bucket(bucket_name)
    gcs_blobs = list(bucket.list_blobs())
    gcs_map = {blob.name: blob for blob in gcs_blobs} 
    
    el_docs = get_elevenlabs_docs()
    
    valid_docs = []
    ids_to_delete = []

    for filename, blob in gcs_map.items():
        if filename in el_docs:
            if filename == triggered_file_name and event_type == "google.cloud.storage.object.v1.finalized":
                print(f"File {filename} changed. Uploading new version...")
                new_id = upload_file_to_elevenlabs(bucket_name, filename)
                if new_id:
                    valid_docs.append({'id': new_id, 'name': filename})
                    ids_to_delete.append(el_docs[filename])
            else:
                valid_docs.append({'id': el_docs[filename], 'name': filename})
        else:
            print(f"File {filename} is new. Uploading...")
            new_id = upload_file_to_elevenlabs(bucket_name, filename)
            if new_id:
                valid_docs.append({'id': new_id, 'name': filename})

    for filename, doc_id in el_docs.items():
        if filename not in gcs_map:
            print(f"File {filename} removed from bucket. Marking for deletion.")
            ids_to_delete.append(doc_id)

    print(f"Updating Agent to use {len(valid_docs)} documents...")
    update_agent_knowledge(valid_docs)

    if ids_to_delete:
        print(f"Deleting {len(ids_to_delete)} orphaned documents...")
        for doc_id in ids_to_delete:
            delete_elevenlabs_doc(doc_id)

    return "Sync complete"
