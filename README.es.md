<p align="center">
  <img src="assets/readme/hero.svg" width="100%" alt="Argo: búsqueda multi-motor y verificación de evidencia para agentes de IA">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh.md">中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a> ·
  <strong>Español</strong>
</p>

<p align="center">
  <a href="https://github.com/taxueseek/argo/stargazers"><img alt="stars" src="https://img.shields.io/github/stars/taxueseek/argo?style=social"></a>
  <a href="https://www.npmjs.com/package/argo-search"><img alt="npm downloads" src="https://img.shields.io/npm/dm/argo-search?color=cb3837&label=npm%20downloads"></a>
  <a href="https://skills.sh/taxueseek/argo"><img alt="skills.sh" src="https://img.shields.io/badge/skills.sh-argo-black"></a>
  <img alt="license" src="https://img.shields.io/badge/license-Apache_2.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.9+-green">
  <img alt="version" src="https://img.shields.io/badge/version-2.14.1-informational">
  <img alt="engines" src="https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Ftaxueseek%2Fargo%2Fmain%2Fassets%2Freadme%2Fcounts.json">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-19%20tools-purple">
</p>

> **Serie de skills taxueseek** (búsqueda e investigación profunda): [plainspoken-writing](https://github.com/taxueseek/plainspoken-writing) 75★ · [session-digger](https://github.com/taxueseek/session-digger) 34★ · [taxue-invest](https://github.com/taxueseek/taxue-invest) 30★ · [taxueskills](https://github.com/taxueseek/taxueskills) 25★ · [taxue-creative-style](https://github.com/taxueseek/taxue-creative-style) 19★ · [ime-lexicon](https://github.com/taxueseek/ime-lexicon) 15★ | **Plugins DSH**: [dsh-files](https://github.com/taxueseek/dsh-files) 43★ · [plugin-fast-adapt](https://github.com/taxueseek/plugin-fast-adapt) 15★ · [dsh-plugin-guard](https://github.com/taxueseek/dsh-plugin-guard) 2★

**Argo** es una herramienta de código abierto para búsqueda multi-motor, extracción web y verificación de evidencia diseñada para agentes de IA. Funciona **sin necesidad de claves API de pago** mediante respaldo local gratuito, enruta consultas en **18 idiomas** y verifica hechos directamente sobre el texto completo de las fuentes.

---

## Inicio Rápido

- **Claude Code (MCP)**:
  ```bash
  claude mcp add argo -- npx -y argo-search
  ```
- **Agent Skill (`skills.sh` — Claude Code / Cursor / Codex)**:
  ```bash
  npx skills add taxueseek/argo
  ```
- **Cursor / Windsurf (`mcpServers` JSON)**:
  ```json
  {
    "mcpServers": {
      "argo": { "command": "npx", "args": ["-y", "argo-search"] }
    }
  }
  ```
- **CLI Local**:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
  ```

---

## Por qué Argo

| Capacidad | Búsqueda Integrada | APIs Comerciales (Tavily / Exa) | Argo |
| :--- | :--- | :--- | :--- |
| **Costo y Claves** | Limitado al host | Pago por uso · clave obligatoria | **Respaldo gratuito sin clave** (claves opcionales mejoran calidad) |
| **Enrutamiento** | Índice genérico único | Índice web general | **Por dominio y 18 idiomas** (académico, código, finanzas) |
| **Verificación** | Solo fragmentos | Fragmentos o texto bruto | **`--verify` descarga la fuente y calcula puntuación de evidencia** |
| **Búsqueda Local** | No | No | **Archivos locales + notas semánticas (`local-seek`)** |

```bash
# Ejemplo: consulta en español con verificación de fuentes y salida compacta
argo search "arquitectura de agentes RAG 2026" --verify 2 --json --fields agent
```

---

## Capacidades y Documentación

- **Agregación de Búsqueda IA**: combina canales gratuitos sin clave (`tavily_free`, `felo`, `parallel_free`) con **DeepSeek Search**, **Tencent Yuanbao (Hunyuan)**, **Metaso**, **Bocha**, **Tavily** y **Exa** bajo un mismo enrutador.
- **19 herramientas MCP** (`argo_search`, `argo_fetch`, `argo_research`, `argo_evidence`, `argo_local_search`, etc.) y 4 sub-habilidades (`local-search`, `local-seek`, `ego-search`, `js-run`).
- **Plugin DSH** (`dsh plugin --profile web add argo-search`) con 18 herramientas nativas y orquestación paralela ([packages/dsh-plugin/README.md](packages/dsh-plugin/README.md)).
- Documentación completa: [docs/ENGINE_CATALOG.md](docs/ENGINE_CATALOG.md) · [docs/MCP_SETUP.md](docs/MCP_SETUP.md) · [docs/CHANGELOG.md](docs/CHANGELOG.md)

| Versión | Novedades |
| :--- | :--- |
| **v2.14.1** | Adelgazamiento de arquitectura, herramientas MCP 3+1 y alias compatibles, 304 fuentes, redacción `secret_guard.py` |
| **v2.14.0** | Consolidación del parser, 300 fuentes, ciclo de registro de fallos |
| **v2.13.0** | Consolidación documental, máquina de 22 estados SSOT, gracia de despacho |

<details>
<summary>Resumen de la versión v2.14.1</summary>

### Capacidades actuales (v2.14.1)

v2.14.1 simplifica la arquitectura de fuentes, añade fuentes sin clave, consolida las herramientas MCP 3+1 con alias compatibles e incorpora redacción de seguridad `secret_guard.py`.

### v2.14.1: Adelgazamiento de Arquitectura + Herramientas MCP 3+1 + Redacción de Seguridad

Consulta [docs/RELEASE_NOTES_v2.14.1.md](docs/RELEASE_NOTES_v2.14.1.md) para más detalles.

</details>
