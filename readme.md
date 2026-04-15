# 📊 Excel Sheet Analyst - Version 2

This production-grade agent uses **Gemini 2.5 Flash** and **DuckDB** to perform high-performance SQL analysis on Google Drive spreadsheets. It is optimized for the **Vertex AI Agent Engine (Reasoning Engine)** using the Google ADK framework. You can easily interact with it by integrating into Gemini Enterprise.

---

## 🛡️ Key Features
* **Artifact-Based Data Passing**: Uses ADK artifacts to securely pass large datasets to the code execution agent for charting.
* **Atomic Data Chain**: Forced "Download → Query" flow in `prompts.py` ensures data is analyzed before serverless instances reset.
* **SQL Robustness**: Uses `ILIKE` with wildcard gap-mapping to handle messy human-entered text in spreadsheets.
* **In-Memory Processing**: Leverages DuckDB for lightning-fast analysis of large CSVs and Excel files directly in RAM.

---


## 📂 Codebase Structure
The agent uses a modular architecture. All files must remain in the root directory for the ADK deployment to package them correctly.

```text
.
├── agent.py          # ADK Agent definition & tool binding
├── auth.py           # OAuth2 token extraction & Drive service builder
├── config.py         # Env validation & centralized logging
├── engine.py         # DuckDB logic & SQL execution
├── prompts.py        # System instructions & SQL strategies
├── tools.py          # Flat wrappers for AI tool-calling
├── requirements.txt  # Pinned production dependencies
└── .env              # Local environment variables
```

---

## 🏗️ Google Cloud Console Configuration
Before deploying, follow these navigation steps in the [Google Cloud Console](https://console.cloud.google.com/) to prepare your infrastructure.

### 1. Enable Required APIs
* Navigate to **APIs & Services > Library**.
* Search for and **Enable** the following two APIs:
    1.  `Vertex AI API` (`aiplatform.googleapis.com`)
    2.  `Google Drive API` (`drive.googleapis.com`)

### 2. Create OAuth 2.0 Credentials
* Navigate to **APIs & Services > OAuth Consent Screen**. Configure it for your organization.
* Go to **Credentials > Create Credentials > OAuth Client ID**.
* Select **Web Application**.
* Note your **Client ID** and **Client Secret**.

---

## ⚙️ Environment Configuration

Ensure your local `.env` file is populated with your project details. These variables will be read by the ADK during the deployment process.

```bash
# --- Google Cloud Platform Config ---
GOOGLE_CLOUD_PROJECT=""
GOOGLE_CLOUD_LOCATION=""

# --- Gen AI SDK Configuration ---
# This forces the Unified SDK to use the Vertex AI backend (Enterprise)
GOOGLE_GENAI_USE_VERTEXAI="True"


# --- OAuth 2.0 for End-User Drive Access ---
GOOGLE_CLIENT_ID=""
GOOGLE_CLIENT_SECRET=""
```

## 🚀 Deploying the Agent on Gemini Enterprise

Once you are sure of the code and the output, you can get ready for the deployment. 

This section provides a comprehensive walkthrough for deploying an AI agent within the Gemini
Enterprise ecosystem. It focuses on the specific IAM roles and service permissions required for
reasoning services, data integration with Google Sheets, and storage access.

### STEP 1: Activate your python environment
Activate your python virtual environment


```bash
python3 -m venv .venv       #Python virtual environment creation
source .venv/bin/activate  #Activate the virtual environment
pip3 install -r requirements.txt #install required dependencies
cd EXCEL_SHEET_ANALYSIS_AGENT_V1
```

### Deploy the agent in Vertex AI Agent Engine
```bash
adk deploy agent_engine \
  --display_name="Excel Analysis Agent" .
```
Wait for it to successfully deploy. Copy the **resource ID** you will see on the screen after a successful deployment for later use.

### STEP 2: Provisioning the permissions to Vertex AI Reasoning Service Account
The agent deployed on the Vertex AI Agent Engine uses the Vertex AI Reasoning default Service Account.

1. Go to Google cloud console, IAM
2. Tick the check box on top right "Include Google-provided role grants"
3. Locate the account with email : `service-PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com`. (If you don't see this account, ensure you have run adk deploy at least once.)
4. Edit permissions, Add `Service Usage Consumer` (Storage permissions are no longer needed as we use ADK Artifacts).


## Create Gemini Enterprise App
1. In the Google Cloud console, go to the [Gemini Enterprise page](https://console.cloud.google.com/gemini-enterprise/start).
2. On the **Apps** page, click **Create app**.
3. In the App name field, enter a name for your app.
4. In the Choose a location section, select a Multi-region.
5. Click **Create.**

### STEP 3: Provide Gemini enterprise app to use user's Authentication
1. In the Google Cloud console, go to the [Gemini Enterprise page](https://console.cloud.google.com/gemini-enterprise/start).
2. Click **Settings**, **Authentication** tab
3. Under Location global, click edit and select 	**Google Identity**
4. Click **Save**.


### STEP 4: Registering tp the Agent we deployed on Vertex AI Agent Engine
1. In the Google Cloud console, go to the [Gemini Enterprise page](https://console.cloud.google.com/gemini-enterprise/start).
2. Select the App you created, Click "+ Add agents"
3. Click "Custom agent via Agent Engine*
4. In the Authorization, click on Add Authorization and provide the below details:
   - **Authorizartion Name**: Any Name (Ex: excel-analysis-agent-auth)
   - **Client ID**: Provide the client id received from step - Create OAuth 2.0 Credentials
   - **Client Secret**: Provide the client secret received from step - Create OAuth 2.0 Credentials
   - **Token URI**:https://oauth2.googleapis.com/token
   - **Authentication URI**:https://accounts.google.com/o/oauth2/v2/auth?client_id=YOUR_CLIENT_ID&redirect_uri=https%3A%2F%2Fvertexaisearch.cloud.google.com%2Fstatic%2Foauth%2Foauth.html&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive&include_granted_scopes=true&response_type=code&access_type=offline&prompt=consent

5. Provide a name and description of the agent, which will be displayed in your Gemini enterprise UI. 
6. Add the resource ID you saved earlier in Step 1. (i.e. projects/.../reasoningEngines/...)
7. Click **Create** 

You will see a new agent is added and Enabled.
1. Click on the Agent name, go to "User permission tab"
2. Click "+Add agent"
3. You can choose your preferred choice, if this agent will be exposed to every user of Gemini Enterprise app then select "All Users", Assign Role "Agent User"
4. Click **Save**

### Step 6 Run the pipeline
1. In the Google Cloud console, go to the [Gemini Enterprise page](https://console.cloud.google.com/gemini-enterprise/start).
2. Click 'Agents' on the left, Select you agent showing down the page. 
3. You will land onto your agent's chat window. 
4. Provide the Google Drive File ID or Google Sheet Link to the agent and ask your question (e.g., "Analyze the data in file <ID>").
5. The agent will process the data and provide insights. If charts are needed for large datasets, it will use ADK artifacts.

---


