# 🚀 Guida di Avvio - Universal Robots Lab Platform

Questa guida spiega come avviare e utilizzare la piattaforma su macOS, Linux e Raspberry Pi.

---

## ⚡ Avvio Rapido (Ora che l'ambiente è configurato)

L'ambiente virtuale (`.venv`) è già stato creato e tutte le dipendenze necessarie (`uvicorn`, `fastapi`, `ur_rtde`, `pyniryo`, ecc.) sono state installate.

Per avviare il server, apri il terminale in questa cartella ed esegui:

```bash
./run.sh
```
*(oppure `bash run.sh`)*

### In modalità sviluppo (ricarica automatica al salvataggio dei file):
```bash
./run.sh --reload
```

---

## 🌐 Indirizzi di Accesso

Una volta avviato lo script, vedrai a terminale gli indirizzi per collegarti:

| Interfaccia | URL Locale | URL Rete LAN (per gli studenti) |
| :--- | :--- | :--- |
| **Dashboard Docente / Admin** | [http://localhost:8000/admin](http://localhost:8000/admin) | `http://<IP_DEL_MAC>:8000/admin` |
| **Interfaccia Studente** | [http://localhost:8000](http://localhost:8000) | `http://<IP_DEL_MAC>:8000` |

> 💡 Lo script `./run.sh` mostrerà automaticamente a video gli indirizzi IP esatti da comunicare agli studenti collegati alla stessa rete WiFi/Ethernet.

---

## 🛑 Come Fermare il Programma

Nel terminale dove è attivo il server, premi la combinazione di tasti:
```text
CTRL + C
```

---

## 🔧 Avvio Manuale (Alternativa senza run.sh)

Se preferisci attivare l'ambiente ed eseguire il comando uvicorn manualmente:

```bash
# 1. Attiva l'ambiente virtuale
source .venv/bin/activate

# 2. Avvia il server Uvicorn
uvicorn app:app --host 0.0.0.0 --port 8000
```

---

## ❓ Perché si era verificato l'errore precedente?

Quando hai eseguito `sh run.sh`:
1. **Ambiente virtuale assente**: Il progetto necessita di un virtual environment Python per isolare i pacchetti.
2. **`pip: command not found`**: Su macOS il comando di sistema non è `pip` ma `pip3` oppure richiede un ambiente virtuale attivo (`source .venv/bin/activate`).
3. **Python Xcode predefinito**: Senza `.venv`, lo script richiamava il Python di default del sistema (`/Applications/Xcode.app/.../python3`), che non conteneva `uvicorn`.

### Cosa è stato risolto:
- È stato creato l'ambiente virtuale isolato in `.venv`.
- Sono state installate tutte le dipendenze da [requirements.txt](file:///Users/mike/Documents/GitHub/DWE-thermo-crio-UI/controllo-robots-itis/requirements.txt).
- È stato reso più robusto [run.sh](file:///Users/mike/Documents/GitHub/DWE-thermo-crio-UI/controllo-robots-itis/run.sh), in modo da rilevare ed usare automaticamente l'interprete `.venv/bin/python` anche se eseguito tramite `sh run.sh`.

---

## 🛠️ Ripristino o Nuova Installazione (Se sposti il progetto)

Se dovessi clonare o reinstallare il progetto su un altro computer o dispositivo:

```bash
# 1. Crea l'ambiente virtuale
python3 -m venv .venv

# 2. Attiva l'ambiente
source .venv/bin/activate

# 3. Aggiorna pip e installa i requisiti
pip install --upgrade pip
pip install -r requirements.txt

# 4. Avvia
./run.sh
```
