"""RemoteDesk Técnico: janela própria para acessar o PC do cliente."""
import webview

from config import server_url

webview.create_window("RemoteDesk Técnico", server_url(), width=1100, height=720)
webview.start()
