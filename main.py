import os
import json
import requests
import tempfile
from google.cloud import storage
import functions_framework

# CONFIG & CONSTANTS
ELEVENLABS_API_KEY = os.getenv('XI_API_KEY')
ELEVENLABS_AGENT_ID = os.getenv('AGENT_ID')
ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1"

# Initialize Storage Client
storage_client = storage.Client()

def get_elevenlabs_docs():
    """Fetches all documents currently in the ElevenLabs Knowledge Base."""
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    documents = {} # Mapping: filename -> doc_id
    
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
    """Downloads file from GCS and uploads to ElevenLabs. Returns the new ID."""
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    
    _, temp_local_filename = tempfile.mkstemp()
    
    try:
        blob.download_to_filename(temp_local_filename)
        headers = {"Xi-api-key": ELEVENLABS_API_KEY}
        args = {'name': blob_name} 
        
        with open(temp_local_filename, 'rb') as f:
            files = {'file': (blob_name, f, 'text/plain')}
            response = requests.post(
                f"{ELEVENLABS_API_URL}/convai/knowledge-base/file",
                headers=headers,
                data=args,
                files=files
            )
            
        if response.status_code == 200:
            data = response.json()
            print(f"Uploaded {blob_name}, ID: {data['id']}")
            return data['id']
        else:
            print(f"Failed to upload {blob_name}: {response.text}")
            return None
    finally:
        if os.path.exists(temp_local_filename):
            os.remove(temp_local_filename)

def update_agent_knowledge(valid_docs):
    """
    Updates the agent to use the new list of documents.
    Args:
        valid_docs (list): A list of dicts [{'id': '...', 'name': '...'}, ...]
    """
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }

    # 1. Try to fetch current agent to handle broken states
    try:
        get_resp = requests.get(
            f"{ELEVENLABS_API_URL}/convai/agents/{ELEVENLABS_AGENT_ID}",
            headers=headers
        )
        if get_resp.status_code != 200:
            print(f"Warning: Could not fetch agent ({get_resp.status_code}). Proceeding with overwrite.")
    except Exception as e:
         print(f"Exception fetching agent: {e}. Proceeding with overwrite.")

    # 2. Construct the New Config
    # FIX: We now include the "name" field which is required by the API
    new_kb_config = []
    for doc in valid_docs:
        new_kb_config.append({
            "type": "file",
            "id": doc['id'],
            "name": doc['name'], # <-- THIS WAS MISSING
            "usage_mode": "auto"
        })

    # 3. Patch the agent
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

    # 1. INVENTORY
    bucket = storage_client.bucket(bucket_name)
    gcs_blobs = list(bucket.list_blobs())
    gcs_map = {blob.name: blob for blob in gcs_blobs} 
    
    el_docs = get_elevenlabs_docs() # {filename: doc_id}
    
    valid_docs = [] # List of {'id': ..., 'name': ...}
    ids_to_delete = []

    # 2. UPLOAD / IDENTIFY VALID DOCS
    for filename, blob in gcs_map.items():
        if filename in el_docs:
            # File exists in both.
            # Check if this specific file triggered the update
            if filename == triggered_file_name and event_type == "google.cloud.storage.object.v1.finalized":
                print(f"File {filename} changed. Uploading new version...")
                new_id = upload_file_to_elevenlabs(bucket_name, filename)
                if new_id:
                    valid_docs.append({'id': new_id, 'name': filename})
                    ids_to_delete.append(el_docs[filename]) # Delete old version
            else:
                # No change, keep existing
                valid_docs.append({'id': el_docs[filename], 'name': filename})
        else:
            # New file
            print(f"File {filename} is new. Uploading...")
            new_id = upload_file_to_elevenlabs(bucket_name, filename)
            if new_id:
                valid_docs.append({'id': new_id, 'name': filename})

    # 3. IDENTIFY REMOVALS
    for filename, doc_id in el_docs.items():
        if filename not in gcs_map:
            print(f"File {filename} removed from bucket. Marking for deletion.")
            ids_to_delete.append(doc_id)

    # 4. UPDATE AGENT
    print(f"Updating Agent to use {len(valid_docs)} documents...")
    update_agent_knowledge(valid_docs)

    # 5. DELETE OLD DOCUMENTS
    if ids_to_delete:
        print(f"Deleting {len(ids_to_delete)} orphaned documents...")
        for doc_id in ids_to_delete:
            delete_elevenlabs_doc(doc_id)

    return "Sync complete"