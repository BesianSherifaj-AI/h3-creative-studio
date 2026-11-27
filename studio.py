"""Launch H3 Creative Studio in its own Windows window."""
import argparse
import logging
import os
import sys
import threading
from pathlib import Path

from h3studio.application import Studio
from h3studio.server import Server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path)
    parser.add_argument('--home', type=Path, default=Path(os.getenv('LOCALAPPDATA',Path.home()))/'H3 Creative Studio')
    parser.add_argument('--serve', action='store_true', help='Use the printed private loopback URL for development.')
    parser.add_argument('--port',type=int,default=0)
    args = parser.parse_args()
    args.home.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(filename=args.home/'desktop.log',level=logging.WARNING)
    studio = Studio(args.home,args.project)
    server = Server(studio,args.port)
    try:
        if args.serve:
            print(server.url, flush=True)
            server.serve_forever()
        else:
            import webview
            class Dialogs:
                def choose(self,kind,filename=''):
                    mode = {'file':webview.FileDialog.OPEN,'folder':webview.FileDialog.FOLDER,'save':webview.FileDialog.SAVE}[kind]
                    value = window.create_file_dialog(mode,save_filename=filename or '')
                    if not value:
                        return ''
                    return str(value[0] if isinstance(value,(list,tuple)) else value)
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            window = webview.create_window('H3 Creative Studio',server.url,js_api=Dialogs(),
                                           width=1440,height=920,min_size=(880,635),background_color='#121417')
            webview.start(gui='edgechromium',private_mode=True)
            server.shutdown()
    finally:
        server.server_close()
        studio.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        logging.exception('Desktop startup failed')
        if sys.platform=='win32' and Path(sys.executable).stem.lower()=='pythonw':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,str(error),'H3 Creative Studio',16)
        else:
            raise
