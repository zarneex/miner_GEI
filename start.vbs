' Запуск панели без окна консоли: терминал можно закрывать — приложение живёт.
' Двойной клик по этому файлу = то же, что «pythonw main.py», но без окна.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\Admin\DSH\майнинг\miner"
sh.Run """C:\Python314\pythonw.exe"" main.py", 0, False
