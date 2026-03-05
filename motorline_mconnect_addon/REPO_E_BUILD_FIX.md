# Correção dos 3 erros (repo + build)

## 1. `fatal: could not read Username for 'https://github.com'`

**Causa:** Repositório **privado** — o Supervisor não consegue clonar sem credenciais.

**Solução:** No GitHub → **ricard0g0mes/motorline** → **Settings** → **General** → **Danger Zone** → **Change visibility** → **Make public**.

---

## 2. `https://github.com/ricard0g0mes/motorline is not a valid add-on repository`

**Causa:** Falta o ficheiro **repository.yaml na raiz** do repositório (não dentro da pasta do addon).

**Estrutura obrigatória no GitHub:**

```
motorline/
├── repository.yaml              ← na raiz (junto a motorline_mconnect_addon)
└── motorline_mconnect_addon/
    ├── config.yaml
    ├── build.yaml
    ├── Dockerfile
    ├── run.sh
    ├── README.md
    └── app/
        ├── main.py
        ├── requirements.txt
        └── options.json
```

**Conteúdo de `repository.yaml`** (na raiz do repo):

```yaml
name: Motorline MConnect
url: https://github.com/ricard0g0mes/motorline
maintainer: ricard0g0mes
```

---

## 3. `An unknown error occurred while trying to build the image for addon`

**Causa:** O build da imagem Docker falhou. As alterações feitas:

- **build.yaml:** Passou a usar a base Alpine normal (`*-base:3.19`) em vez de `*-base-python`, que pode não existir ou falhar em algumas arquiteturas.
- **Dockerfile:** Usa `FROM ${BUILD_FROM}`, instala Python com `apk add python3 py3-pip` e copia os ficheiros de forma explícita (`COPY app/*.py ./` para não depender de pasta `app` inteira se o contexto de build for diferente).

**O que fazer:**

1. Substitui no teu repo **build.yaml** e **Dockerfile** pelos que estão nesta pasta.
2. No HA, **remove o repositório** (Add-ons → ⋮ → Repositórios → eliminar o URL do motorline).
3. Reinicia o Supervisor (Definições → Sistema → Reiniciar → Reiniciar Supervisor) ou espera uns minutos.
4. Adiciona de novo: `https://github.com/ricard0g0mes/motorline`.
5. Instala outra vez o addon **Motorline MConnect**.

**Para ver o erro exato do build:** No host do HA (SSH ou Terminal e Add-on “Terminal & SSH”), corre:

```bash
ha supervisor logs
```

Ou em **Definições** → **Sistema** → **Registos** (Supervisor). Procura linhas com “build” ou “error” ao tentar instalar o addon.
