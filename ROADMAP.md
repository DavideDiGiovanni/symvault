# Vault – Sviluppi Futuri

## Priorità alta (adozione pubblica)

### LICENSE
Aggiungere MIT o Apache-2.0. Senza licenza nessuno può legalmente usare/forkare il progetto.

### CI (GitHub Actions)
Workflow che esegue pytest su push. Badge verde nel README per dare fiducia.

### README in inglese
L'attuale è in italiano — limita il pubblico. Servono almeno README inglese + Quick Start con caso d'uso reale (es. "50GB di foto con duplicati → 30GB").

### Pubblicazione PyPI
`python -m build && twine upload` per rendere funzionante `pipx install vault-dedup`.

### Benchmark
Paragone con fdupes/jdupes/rdfind su dataset reale. Risponde a "quanto spazio risparmio?" e "quanto è veloce?".

### Nome comando
"vault" collide con HashiCorp Vault. Valutare rename (es. `symvault`, `vlt`) o documentare la coesistenza.

## Priorità media

---

## Priorità bassa

### `--exclude` inline
Oltre a `.symvaultignore`, supportare `--exclude "*.mp4"` direttamente nel comando scan. ✅ Completato.

### `status --json`
Output machine-readable per scripting e integrazione con altri tool.

### `scan --stats-only`
Mostra quanti file verrebbero processati senza calcolare hash. Più veloce di `--dry-run`.

### Export/import DB
Esportare il DB come JSON per backup o migrazione tra vault diversi.

### `watch` (monitoring real-time)
Demone con `watchdog` che monitora le cartelle e deduplica automaticamente i nuovi file. Aggiunge complessità (processo in background, gestione crash).

### `-q` (quiet mode)
Solo riepilogo finale, nessun output intermedio.

---

## Completati

- ✅ Lock file (`fcntl.flock` su `.symvault/lock`, solo operazioni mutanti)
- ✅ Progress bar (`click.progressbar` con contatore, senza filename per evitare wrapping)
- ✅ Graceful Ctrl+C (signal handler, commit parziale, nessuno stato intermedio)
- ✅ Colori output (verde/giallo/rosso/blu/ciano/magenta con `click.style()`)
- ✅ `verify --fix` (auto-repair di rename, deleted, untracked, broken, orphan, unreferenced)
- ✅ Rename tracking (rilevamento automatico in `verify` via hash matching)
- ✅ Tab completion per comandi e path (zsh/bash)
- ✅ Script `install.sh` / `uninstall.sh`
- ✅ `dupes` — mostra gruppi di file duplicati (stesso hash, più symlink)
- ✅ `-v` / `--verbose` su scan — mostra file skippati e motivo (dopo la progress bar)
- ✅ `-v` / `--verbose` su status, revert, rebuild — dettagli duplicati/orfani, file revertiti, symlink creati
- ✅ Symlink assoluti (sopravvivono a spostamento/rinomina dei singoli file)
- ✅ `os.scandir()` al posto di `os.walk()` + `stat()` (stat cached, performance su NFS)
- ✅ Indice DB su `files.size` (performance con molti blob)
- ✅ Gestione disco pieno (try/except su copy2, cleanup file parziale, scan continua)
- ✅ Safe revert (copia su temp file, poi rename; nessuna perdita dati su errore)
- ✅ Broken symlinks in verify (symlink a blob mancante, distinti da untracked)
- ✅ `rebuild` dual mode (senza dest: in-place; con dest: ricrea + aggiorna DB)
- ✅ `.symvaultignore` hardcoded (escluso dalla raccolta candidati, non solo da pattern)
- ✅ Fix lock leak in verify (release su tutti i return path)
- ✅ Fix `relative_to` crash su path esterni (delete/revert)
- ✅ Fix `resolve()` seguiva symlink in revert/delete (usato `absolute()`)
- ✅ Versione Windows (`vault_win.py` + `README_WIN.md`, locking con `msvcrt`)
- ✅ `pyproject.toml` per installazione via pip/pipx
- ✅ Test suite pytest (17 test, copertura completa dei 16 casi README)
- ✅ Conferma interattiva su `revert` senza argomenti (`-y` per saltare)
- ✅ Preload links/files in memoria durante scan (zero SELECT per file)
- ✅ Verify ottimizzato con cache mtime/size sui blob (skip hash se invariato)
- ✅ Buffer SHA-256 da 1MB (riduce syscall su file grandi)
- ✅ `PRAGMA synchronous=NORMAL` (meno fsync, rischio solo su power loss)
- ✅ Pattern .symvaultignore precompilati con `re.compile`
- ✅ `os.scandir` in `_find_vault_symlinks` (coerenza con scan)
- ✅ Rimosso `sha256_partial` (codice morto)
- ✅ Rimosso `executescript(SCHEMA)` ridondante (solo in init)
- ✅ `gc` — garbage collect dedicato (blob orfani, link stale, shard vuote)
- ✅ `list <hash>` — mostra tutti i symlink che puntano a un blob (supporta prefisso)
- ✅ `--exclude` inline su scan — pattern glob ripetibili per esclusioni one-off
- ✅ `cp` — esportazione file dal vault come file reali (singoli, glob, cartelle ricorsive, `--dry-run`, `--no-overwrite`, `-v`)

---

## Scartati (con motivazione)

### Two-phase hashing (partial → full)
Analizzato e scartato: il full SHA-256 è sempre necessario perché è il nome del blob. L'ottimizzazione mtime/size/inode è più efficace.

### Parallelismo (multiprocessing per hashing)
Il bottleneck è quasi sempre l'I/O disco, non la CPU. Aggiunge complessità senza beneficio reale su storage locale.

### Compressione dei blob
Complicherebbe il revert e i symlink non funzionerebbero più come accesso diretto ai file.

### Ownership (uid/gid) tracking
`shutil.copy2` preserva già permessi e timestamp. L'ownership richiederebbe root per il restore. Non necessario per l'uso previsto (file personali, stesso utente).
