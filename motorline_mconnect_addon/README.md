# Motorline MConnect – Addon Home Assistant

Addon que faz de **proxy** para a API Motorline MConnect: **login automático ao arranque**, **painel web** para introduzir o código enviado por email, obtenção automática do dispositivo (quando possível) e **verificação horária do token** com aviso para novo código quando a sessão expira.

## O que faz

- **Ao instalar**: Configuras email e password nas opções do addon; ao iniciar, o addon tenta login de forma transparente. Se a API enviar código por email, abre o **painel web** (http://&lt;addon&gt;:8765/) e introduces o código na janela. O **device_id** é obtido automaticamente quando há um único dispositivo.
- **Renovação de token**: em 401 ou N segundos antes do fim da validade (`refresh_before_expiry_seconds`).
- **Verificação a cada hora**: o addon confirma se o token ainda é válido; se não for, alerta no painel e pede novo código por email (introdução na mesma janela).
- **Endpoint local**: `http://<addon>:8765/trigger` — comando do portão.

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

## Utilização (sem configuração no HA)

1. **Instala** o addon e **inicia-o**. Não é necessário preencher nenhuma opção nas definições do addon.
2. Abre o **painel web**: **Add-ons** → **Motorline MConnect** → **Abrir painel web** (ou `http://<addon>:8765/`).
3. No **primeiro arranque** o painel pede **email** e **password** da conta Motorline MConnect. Introduz e clica em **Iniciar sessão**.
4. Se a API enviar um **código por email**, o painel mostra o campo para o código. Introduz o código recebido e clica em **Submeter código**.
5. O **device_id** é obtido automaticamente (um dispositivo) ou guardado o primeiro da lista. Ficas **Operacional**.
6. **Quando a sessão expirar** (verificação a cada hora): o painel mostra aviso e volta a pedir o código; introduz o novo código no mesmo painel.

Tudo (credenciais, device_id) fica guardado em `/data/` dentro do addon. A URL da API e o intervalo de renovação são internos.

## Endpoints do addon (porta 8765)

- **GET/POST** `http://<host>:8765/trigger` ou `/command`  
  Dispara o portão (valor default 1).  
  Opcional: `?value=1` ou body `{"value": 1}`.

- **PUT/POST** `http://<host>:8765/device/value`  
  Body: `{"value": 1}`. Usa o `device_id` da configuração.

- **GET** `http://<host>:8765/health`  
  Verificação de estado (responde `{"status":"ok"}`).

- **GET** `http://<host>:8765/`  
  **Painel web**: estado do login, introdução do código e aviso quando a sessão expira.

- **GET** `http://<host>:8765/login/status`  
  Indica se está à espera do código (`awaiting_code`), ativo (`ready`) ou não autenticado.

- **POST** `http://<host>:8765/login/start`  
  Dispara o fluxo de login (usa email/password das opções). O painel chama isto ao carregar se não estiver autenticado.

- **POST** `http://<host>:8765/login/verify`  
  Body: `{"code": "123456"}` — submete o código recebido por email para concluir o login.

- **GET** `http://<host>:8765/api/ui-state`  
  Estado para o painel: `status`, `token_expired_alert`, `device_id`.

- **GET** `http://<host>:8765/api/devices`  
  Lista dispositivos (requer token).

- **POST** `http://<host>:8765/api/device_id`  
  Body: `{"device_id": "..."}` — guarda o ID do dispositivo (persistente em `/data/`).

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

### 2) Submeter código de verificação (quando o login pede código por email)

Adiciona um `input_text` para o código e um REST command que envia o código ao addon:

```yaml
# configuration.yaml
input_text:
  motorline_codigo_email:
    name: Código Motorline (email)
    max: 10

rest_command:
  motorline_portao:
    url: "http://a0d7b954_motorline_mconnect:8765/trigger"
    method: POST
    content_type: "application/json"
    payload: '{"value": 1}'
  motorline_submeter_codigo:
    url: "http://a0d7b954_motorline_mconnect:8765/login/verify"
    method: POST
    content_type: "application/json"
    payload_template: '{"code": "{{ states(''input_text.motorline_codigo_email'') }}"}'
```

Fluxo: quando o addon pedir o código, abres o email, colas o código em **Entidades** → `input_text.motorline_codigo_email`, e chamas o serviço `rest_command.motorline_submeter_codigo` (por exemplo com um botão no dashboard).

### 3) Botão ou script (portão)

```yaml
# script
script:
  abrir_portao_motorline:
    sequence:
      - action: rest_command.motorline_portao
```

Ou num dashboard: **Entidades** → **Criar botão** → Ação: **Chamar serviço** → `rest_command.motorline_portao`.

### 4) Chamar por URL (navegador ou HTTP)

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
