# AI Agent Handover Document: EFplant Dashboard Security Implementation

## 📌 Project Context (專案背景)
This project (`EFplant`) generates a factory SCADA dashboard (`EFplant_Dashboard.html`) using a Python script (`generate_dashboard.py`). The dashboard is hosted entirely as a **static site on GitHub Pages**. 
Because GitHub Pages cannot run backend code, and the user requires **multi-account password protection (50+ accounts)**, we implemented a highly secure **Static Multi-Password AES Encryption** architecture (similar to how 1Password works).

## 🏗️ Architecture & Security Mechanism (核心加密架構)
To protect the dashboard data on a purely static host without communicating with a local server, the following mechanism is implemented:

### 1. Data Encryption (Backend / Python)
- **Tool**: `generate_dashboard.py` uses `pycryptodome` (Crypto.Cipher.AES, Crypto.Protocol.KDF.PBKDF2).
- **Process**:
  1. Generates a random 32-byte `MASTER_KEY`.
  2. Encrypts the entire plaintext dashboard HTML using AES-256-CBC with the `MASTER_KEY` -> `ENCRYPTED_PAYLOAD`.
  3. Reads `accounts.json` (local only).
  4. For each password, derives a key using **PBKDF2-HMAC-SHA256 (100,000 iterations)** with a random `GLOBAL_SALT`.
  5. Splits the derived key: first 16 bytes act as `INDEX_ID`, last 16 bytes act as the `ENCRYPTION_KEY`.
  6. Encrypts the `MASTER_KEY` with the `ENCRYPTION_KEY` using AES-128-CBC -> `enc_master_key`.
  7. Stores `INDEX_ID` -> `{iv, enc_master_key}` in a `KEY_SAFES` dictionary.
  8. Injects the `ENCRYPTED_PAYLOAD`, `KEY_SAFES`, and CryptoJS logic into a Cyberpunk-styled Login HTML wrapper.

### 2. Data Decryption (Frontend / JS)
- **Tool**: CryptoJS library embedded in `EFplant_Dashboard.html`.
- **Process**:
  1. User inputs password.
  2. JS runs PBKDF2 (100,000 iterations) with the same `GLOBAL_SALT` to derive the key.
  3. Uses `INDEX_ID` to find the user's specific "Key Safe" in the dictionary.
  4. Uses the `ENCRYPTION_KEY` to decrypt the `MASTER_KEY`.
  5. Uses the `MASTER_KEY` to decrypt the `ENCRYPTED_PAYLOAD`.
  6. Injects the decrypted HTML into the DOM via `document.write()`.

## 📂 Key Files & Structure
- `generate_dashboard.py`: The core generation and encryption engine. **If you modify the UI or HTML structure, modify the `full_html` variable inside this script.**
- `accounts.json`: Local plaintext file containing the 50+ passwords. **CRITICAL: NEVER upload this to GitHub. It is in `.gitignore`.**
- `EFplant_Dashboard.html`: The final output file. Contains the encrypted payload and the login UI. This is the only file that goes to GitHub Pages.
- `.gitignore`: Ensures `accounts.json` is ignored.

## 🚨 Critical Rules for Future AI Agents
1. **DO NOT modify the AES or PBKDF2 parameters** (e.g., iterations=100000, lengths, CBC modes) in Python without simultaneously updating the exact corresponding logic in the JS block inside `generate_dashboard.py`.
2. **DO NOT expose plaintext data**: When adding new features or data to the dashboard, ensure it is added to `full_html` *before* the encryption step. Do not put sensitive data in the wrapper HTML.
3. **DO NOT build backend verification**: The user wants to keep this 100% free on GitHub Pages. Do not introduce Flask, Node.js backends, or ngrok tunnels. Keep it purely static.
4. **Environment**: The Python virtual environment (`.venv`) has `pycryptodome` installed. Use `from Crypto...` (not `Cryptodome`).

## 🛠️ How to Test
1. Add/Modify passwords in `accounts.json`.
2. Run `python generate_dashboard.py` using the `.venv`.
3. Open `EFplant_Dashboard.html` locally in a browser.
4. Enter a password to verify decryption works.
