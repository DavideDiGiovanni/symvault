# Symvault – Content-Addressable File Deduplication

Symvault è uno strumento CLI Python per la deduplicazione di file basata su content-addressing. Sostituisce i file duplicati con symlink verso un blob store centralizzato (`.vault/objects/`), risparmiando spazio disco e mantenendo la struttura originale delle cartelle navigabile.

## Come funziona

1. I file vengono hashati (SHA-256) e spostati in `.vault/objects/` con sharding à la git (`ab/cdef...ext`)
2. Al posto del file originale viene creato un symlink al blob nel vault
3. Se due file hanno lo stesso contenuto (stesso hash), un solo blob viene conservato
4. Tutto è tracciato in un DB SQLite (`.vault/vault.db`)

## Installazione

```bash
# Via pip
pip install .

# Oppure via pipx (isolato)
pipx install .
```

## Uso

```bash
# Inizializza il vault nella directory corrente
symvault init

# Scansione e deduplicazione
symvault scan .

# Statistiche
symvault status
```
