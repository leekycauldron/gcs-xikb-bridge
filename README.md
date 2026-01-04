# GC XILabs Knowledge Base Bridge
## Setup

### Google Cloud Secrets Manager:
**AGENT_ID:** The ID of the Agent to be attached to the documents in the knowledge base.<br><br>
**XI_API_KEY:** Eleven labs API Key.<br><br>
*Add these secrets to cloud function environment variables, names stay the same*
### Triggers:
`google.cloud.storage.object.v1.finalized`
`google.cloud.storage.object.v1.deleted`
### Service Account Permissions:
`Eventarc Event Receiver`
`Secret Manager Secret Accessor`
## Run
Attach trigger to bucket being monitored, any additions/deletions/modifications to bucket will propogate to ElevenLabs Knowledge Base and attach to Agent.
