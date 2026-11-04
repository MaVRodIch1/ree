# Сервер: только просмотры + слежение за «Обменом» @trade

`python main.py server` — без меню. Работают только две вещи:

- **просмотры канала** (органично: каждый пост видит случайная часть аккаунтов,
  растянуто на ~16 ч, ночью меньше);
- **«Обмен» @trade**: новые подарки уходят в чат.

Рилс, топ и снайпер в этом режиме не запускаются.

## 1. Сервер

Подойдёт любой VPS на Ubuntu 22.04/24.04: 1 vCPU, 1 ГБ RAM.
Регион лучше взять поближе к тому, где обычно сидят аккаунты
(Европа / Нидерланды / Германия).

## 2. Установка (на сервере)

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
sudo useradd -m -d /opt/reelsio -s /bin/bash reelsio
sudo -iu reelsio
git clone -b claude/telegram-reelsio-claimer-iflpyi https://github.com/MaVRodIch1/ree.git ~/repo
ln -s ~/repo/reelsio-claimer ~/reelsio-claimer
python3 -m venv ~/venv
~/venv/bin/pip install -r ~/reelsio-claimer/requirements-server.txt
exit
```

## 3. Перенести сессии и конфиг (с ПК)

С ПК (PowerShell или терминал), подставь IP сервера:

```bash
scp -r sessions config.json exclude_sessions.txt root@IP:/opt/reelsio/reelsio-claimer/
```

Потом на сервере выстави владельца файлов:

```bash
sudo chown -R reelsio:reelsio /opt/reelsio/reelsio-claimer
```

Если сессия для «Обмена» лежит в `sniper_session/`, скопируй её туда же.

В `config.json` на сервере добавь блок (номер = имя .session без расширения):

```json
  "server": {
    "views": true,
    "swap": true,
    "swap_session": "380XXXXXXXXX"
  }
```

Необязательные настройки просмотров (значения по умолчанию):

```json
  "views_share": [0.55, 0.85],
  "views_spread_hours": 16,
  "views_catchup": 0.3,
  "views_parallel": 3,
  "views_night": [2, 7],
  "views_tz": 3
```

Что они значат:

- `views_share` — какая доля аккаунтов увидит пост (для каждого поста своя, случайная);
- `views_spread_hours` — на сколько часов растягиваются просмотры;
- `views_catchup` — шанс, что аккаунт заодно откроет старый пост, который пропустил;
- `views_parallel` — сколько аккаунтов смотрят одновременно;
- `views_night` / `views_tz` — ночные часы аудитории и её часовой пояс (UTC+3 = Москва).

## 4. Проверка вручную

```bash
sudo -iu reelsio
cd ~/reelsio-claimer && ~/venv/bin/python main.py server
```

В логе должно появиться `Сервер запущен…`, а в чат придёт тестовое уведомление
«Обмена». Останавливается через Ctrl+C.

## 5. Автозапуск (systemd)

```bash
sudo cp /opt/reelsio/reelsio-claimer/deploy/reelsio-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reelsio-server
```

Полезные команды:

```bash
journalctl -u reelsio-server -f          # смотреть лог вживую
sudo systemctl restart reelsio-server    # перезапуск
sudo systemctl stop reelsio-server       # остановить
```

Обновить код:

```bash
sudo -iu reelsio git -C ~/repo pull
sudo systemctl restart reelsio-server
```

## ⚠️ Важно

- **Одну и ту же .session нельзя запускать одновременно на ПК и на сервере.**
  Telegram разорвёт обе, а иногда и разлогинит. Когда сервер запущен, на ПК
  эти сессии в комбо не включай: Рилс выключи, просмотры и «Обмен» тоже.
- IP сервера — датацентровый. Если аккаунты начнут вылетать («Not authorized»),
  поставь прокси той же страны, что и аккаунты, или переноси сессии на сервер
  постепенно. Мёртвые сессии бот сам кладёт в `dead_sessions.txt` и больше не трогает.
- Сессии из `exclude_sessions.txt` (из другого проекта) сервер не трогает.
- План просмотров хранится в `views_plan.json`, поэтому после перезапуска
  просмотры продолжаются с того же места.
