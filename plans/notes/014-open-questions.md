# План 014 — что требует владельца

## 1. Ключ исполнителя на sandbox (блокирует настоящую выкатку)

Исполнитель автовыкатки (`runner-installation` на sandbox) ходит на машины
пользователем `olenchikov.a`. На hub его ключ разрешён, на sandbox — нет:
выкатка «Докроя» останавливается на захвате sandbox с
`Permission denied (publickey)`. Бой при этом не трогается. Нужен пароль sudo
на sandbox, поэтому сама не положила.

Подключение:

    ssh olenchikov.a@161.104.50.20

На машине:

    sudo cat /home/runner-installation/.ssh/id_*.pub >> ~/.ssh/authorized_keys
    sudo -u runner-installation ssh -o BatchMode=yes olenchikov.a@161.104.50.20 true && echo ДОСТУП ЕСТЬ

Успех — последняя строка печатает `ДОСТУП ЕСТЬ`. После этого — Actions →
deploy → Run workflow, unit = `docroi`: в `deployed/hub.json` появится запись
docroi с `"machines": ["sandbox", "hub"]`.

## 2. Откат на бою (демонстрация US-0024)

Парный откат проверен тестами. Настоящий `--rollback hub` сменит версию
«Докроя» в бою на обеих машинах — только по вашему слову, после удачной
выкатки на две машины.

## 3. Токен в README.md

В конце `README.md` открытым текстом лежит GitHub-токен «будильника». Им
сейчас пользовалась, чтобы запускать автовыкатку. Он в git; по инварианту 5
это допустимо до прохода по безопасности, но в README он виден любому, кто
откроет файл.

## 4. Тесты установки под Windows

`py -m unittest discover -s deploy` на этой машине падает: `bash` в
`subprocess` — WSL из System32 с одним дистрибутивом docker-desktop. Гоняла в
контейнере `python:3.14-slim` с git. Либо поставить в WSL обычный
дистрибутив (Ubuntu), либо записать в README, где тесты гоняются.
