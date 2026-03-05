# Motorline MConnect – Addon Home Assistant

Addon que faz de **proxy** para a API Motorline MConnect: trata do **login** e da **renovação automática do token** (evita 401 "The token has expired") e expõe um endpoint local para comandar o portão.

## O que faz

- **Login** na API (`api_base_url` + `/auth/login` ou `/login`) com email/password.
- **Renovação de token**: em 401 ou N segundos antes do fim da validade (`refresh_before_expiry_seconds`).
- **Endpoint local** no addon: `http://<addon>:8765/trigger` — ao chamar, o addon usa o token (renovando se for preciso) e chama a API Motorline para definir o valor do dispositivo (portão).

## Instalação no Home Assistant

### Opção A: Addon local (pasta)

1. Copia a pasta `motorline_mconnect_addon` para o teu repositório de addons do Home Assistant (por exemplo `config/addons/motorline_mconnect` ou o repositório que usas).
2. No HA: **Definições** → **Add-ons** → **Add-on Store** → **⋮** → **Repositórios** (se usares repo) ou **Carregar add-on da pasta** se o teu método for por pasta.
3. Se for repositório: cria um `repository.yaml` na raiz do repo com o conteúdo abaixo e adiciona o URL do repositório no HA.

Exemplo de `repository.yaml` (na raiz do repositório que contém este addon):

```yaml
name: Meu Repo
url: https://github.com/TEU_USER/TEU_REPO
maintainer: Teu Nome
```

4. Instala o addon **Motorline MConnect** e configura (ver abaixo).

### Opção B: Copiar para config/addons

1. Em `config/addons/` cria a pasta `motorline_mconnect`.
2. Copia para dentro dela: `config.yaml`, `build.yaml`, `Dockerfile`, `run.sh` e a pasta `app/` (com `main.py`, `requirements.txt` e, se quiseres, `options.json` como exemplo).
3. No Supervisor, o addon deve aparecer como addon local; instala e configura.

## Configuração do addon

No HA, em **Definições** → **Add-ons** → **Motorline MConnect** → **Configuração**:

| Opção | Descrição |
|--------|-----------|
| `api_base_url` | URL base da API (default: `https://api.mconnect.motorline.pt`) |
| `email` | Email da conta MConnect |
| `password` | Password da conta |
| `device_id` | ID do dispositivo/portão (ex.: `66755146c8a511e8645bd710`) |
| `refresh_before_expiry_seconds` | Renovar token N segundos antes de expirar (default: 300). 0 = só renovar ao receber 401 |

Reinicia o addon após guardar.

## Endpoints do addon (porta 8765)

- **GET/POST** `http://<host>:8765/trigger` ou `/command`  
  Dispara o portão (valor default 1).  
  Opcional: `?value=1` ou body `{"value": 1}`.

- **PUT/POST** `http://<host>:8765/device/value`  
  Body: `{"value": 1}`. Usa o `device_id` da configuração.

- **GET** `http://<host>:8765/health`  
  Verificação de estado (responde `{"status":"ok"}`).

Dentro do HA, o host do addon é normalmente o nome do addon (ex.: `a0d7b954_motorline_mconnect`) ou `localhost` se acederes a partir do próprio HA.

## Integração no Home Assistant

### 1) REST Command (configuration.yaml)

```yaml
rest_command:
  motorline_portao:
    url: "http://a0d7b954_motorline_mconnect:8765/trigger"
    method: POST
    content_type: "application/json"
    payload: '{"value": 1}'
```

(Substitui `a0d7b954_motorline_mconnect` pelo nome real do contentor do addon; podes ver em **Definições** → **Add-ons** → **Motorline MConnect** → **Info** → **Host** / URL do addon.)

### 2) Botão ou script

```yaml
# script
script:
  abrir_portao_motorline:
    sequence:
      - action: rest_command.motorline_portao
```

Ou num dashboard: **Entidades** → **Criar botão** → Ação: **Chamar serviço** → `rest_command.motorline_portao`.

### 3) Chamar por URL (navegador ou HTTP)

```
POST http://<IP_HA>:8765/trigger
```
(Se expuseres a porta 8765 no addon; por segurança preferir usar apenas dentro da rede e, se possível, só via REST command no HA.)

## Validade do token

A API Motorline não está documentada publicamente. O addon tenta:

- Obter `access_token` (ou `token` / `accessToken`) e `expires_in` na resposta do login.
- Se a API devolver `expires_in` em segundos, o addon renova o token **refresh_before_expiry_seconds** antes de expirar.
- Se receber **401 "The token has expired"**, faz novo login e volta a tentar o pedido uma vez.

Se o teu login usar outro endpoint ou formato (ex.: form em vez de JSON), diz qual é a resposta do POST de login (JSON ou cabeçalhos) para adaptar o `main.py` (função `login()`).

## Estrutura do projeto

```
motorline_mconnect_addon/
├── config.yaml       # Metadados e schema do addon
├── build.yaml        # Imagens base por arquitetura
├── Dockerfile
├── run.sh
├── README.md
└── app/
    ├── main.py       # Flask + login + proxy Motorline
    ├── requirements.txt
    └── options.json  # Exemplo de opções (não usado em produção; HA injeta /data/options.json)
```

## Resolução de problemas

- **401 ao chamar a API**: Confirma email/password e que o endpoint de login está correto. Se a Motorline usar outro path (ex. `/v1/auth`), adiciona-o em `main.py` na lista de endpoints em `login()`.
- **Addon não inicia**: Verifica os logs em **Add-ons** → **Motorline MConnect** → **Registo**.
- **Portão não reage**: Confirma o `device_id` (ex.: no URL que usavas antes `.../devices/value/66755146c8a511e8645bd710` o ID é `66755146c8a511e8645bd710`). Se a API esperar outro método (GET em vez de PUT) ou outro body, ajusta `set_device_value()` em `main.py`.
