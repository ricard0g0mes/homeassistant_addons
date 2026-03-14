# Motorline MConnect — Addon Home Assistant

Addon para comandar o portão MConnect (Motorline) a partir do Home Assistant. Usa o **link de partilha** da app: configura uma vez, sem login, sem email, sem códigos.

---

## Instalação

1. Adiciona o repositório em **Definições** → **Add-ons** → **Repositórios**:
   ```
   https://github.com/ricard0g0mes/homeassistant_addons
   ```
2. Instala o addon **Motorline MConnect**.
3. Inicia o addon e abre o **painel** (porta 8766).

---

## Configuração (uma vez)

1. **Na app MConnect (telemóvel):**
   - Abre a app e entra na tua casa.
   - Vai a **Partilhar acesso** / **Criar link de partilha** (ou equivalente).
   - Gera o link e copia-o (tipo `https://mconnect.pt/shareable_link?home_id=...&access_code=...`).

2. **No painel do addon (HA):**
   - Cola o link no campo e clica em **Configurar**.
   - O addon obtém o token e o dispositivo (portão) automaticamente. Não é preciso introduzir ID nem fazer login.

Depois disso o portão fica disponível no HA (painel do addon, MQTT e API).

---

## Uso no Home Assistant

### Painel do addon

No painel (link do addon) tens o estado do portão (fechado/aberto/fechando/abrindo) e os botões **Abrir** e **Fechar**.

### MQTT (opcional)

Nas opções do addon podes ativar MQTT. Com MQTT ativo, o addon publica:

- **Sensor:** estado do portão (por exemplo `sensor.motorline_mconnect_share_estado`).
- **Botões:** abrir e fechar (por exemplo `button.motorline_mconnect_share_abrir`, `button.motorline_mconnect_share_fechar`).

Os dispositivos aparecem em **Definições** → **Dispositivos e serviços** → **MQTT** (ou na integração MQTT), associados ao device “Motorline MConnect Share”.

### API HTTP

- **Estado do portão:** `GET http://HA_IP:8766/api/gate-state`  
  Resposta: `{"ok": true, "state": "fechado", "value": 0, ...}`

- **Abrir portão:** `POST http://HA_IP:8766/trigger` com body `{"value": 2}`  
  Ou: `POST http://HA_IP:8766/trigger` com query `?value=2`

- **Fechar portão:** `POST http://HA_IP:8766/trigger` com body `{"value": 0}` ou query `?value=0`

Útil para automações (REST Command), Node-RED, etc.

---

## Renovar o acesso

Se o link expirar ou deixar de funcionar:

1. Na app MConnect, gera um **novo** link de partilha.
2. No painel do addon, cola o novo link e clica em **Configurar**.

O addon guarda `home_id` e `access_code` e renova o token automaticamente quando expira; só precisas de voltar ao painel se o acesso for revogado ou o link antigo deixar de ser válido.

---

## Resumo

| O quê        | Como |
|-------------|------|
| Configurar  | Uma vez: link de partilha da app → colar no painel → Configurar. |
| Comandar    | Painel do addon, entidades MQTT (se ativado) ou API `/trigger`. |
| Estado      | Painel, sensor MQTT ou `GET /api/gate-state`. |
| Sem login   | Não é necessário email, password nem códigos por email. |
