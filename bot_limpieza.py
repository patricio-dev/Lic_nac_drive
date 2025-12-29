import os
import logging
import gspread
import time
import json
import sys
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- CONFIGURACIÓN ---
class Config:
    # AHORA TOMAMOS LOS VALORES DE LAS VARIABLES DE ENTORNO
    ID_HOJA_CALCULO = os.environ.get("SHEET_ID")
    ID_CARPETA_DRIVE_DESTINO = os.environ.get("DRIVE_FOLDER_ID")
    
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    
    COLUMNA_ID = 2 
    NOMBRE_HOJA_LOG = "PAPELERA_LOG"
    
    # SEGURIDAD: Freno de mano para evitar catástrofes
    MINIMO_IDS_SEGURIDAD = 5 

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def autenticar_google():
    logging.info("🔑 Autenticando (Modo Seguro)...")
    
    # Validamos que existan las configuraciones
    if not Config.ID_HOJA_CALCULO or not Config.ID_CARPETA_DRIVE_DESTINO:
        logging.error("❌ ERROR: Faltan las variables de entorno SHEET_ID o DRIVE_FOLDER_ID.")
        sys.exit(1)

    # Leemos el secreto JSON de la memoria
    json_creds = os.environ.get("GCP_CREDENTIALS")
    if not json_creds:
        logging.error("❌ ERROR: No se encontró la variable GCP_CREDENTIALS.")
        sys.exit(1)

    try:
        creds_dict = json.loads(json_creds)
        creds = Credentials.from_service_account_info(creds_dict, scopes=Config.SCOPES)
        gc = gspread.authorize(creds)
        drive = build('drive', 'v3', credentials=creds)
        return gc, drive
    except Exception as e:
        logging.error(f"❌ Error crítico en autenticación: {e}")
        sys.exit(1)

def obtener_ids_validos(gc):
    """Obtiene los IDs de la hoja principal."""
    try:
        sh = gc.open_by_key(Config.ID_HOJA_CALCULO)
        ws = sh.get_worksheet(0)
        raw_ids = ws.col_values(Config.COLUMNA_ID)[1:] # Ignorar encabezado
        lista_limpia = set([str(x).strip() for x in raw_ids if str(x).strip()])
        return lista_limpia, sh
    except Exception as e:
        logging.error(f"Error leyendo la hoja: {e}")
        return set(), None

def obtener_carpetas_drive(drive_service):
    """Obtiene mapa {Nombre_Carpeta: ID_Drive}."""
    mapa_carpetas = {}
    page_token = None
    try:
        while True:
            q = f"'{Config.ID_CARPETA_DRIVE_DESTINO}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            res = drive_service.files().list(q=q, fields='nextPageToken, files(id, name)', pageToken=page_token, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            for f in res.get('files', []):
                mapa_carpetas[f['name']] = f['id']
            page_token = res.get('nextPageToken', None)
            if page_token is None: break
    except Exception as e:
        logging.error(f"Error leyendo Drive: {e}")
    return mapa_carpetas

def gestionar_hoja_log(sh):
    """Obtiene o crea la hoja de registro de borrados."""
    try:
        ws_log = sh.worksheet(Config.NOMBRE_HOJA_LOG)
    except:
        logging.info("📝 Creando hoja de registro 'PAPELERA_LOG'...")
        ws_log = sh.add_worksheet(title=Config.NOMBRE_HOJA_LOG, rows=1000, cols=2)
        ws_log.append_row(["ID_MP", "ESTADO"])
    
    raw_log = ws_log.col_values(1)[1:]
    return ws_log, set([str(x).strip() for x in raw_log if str(x).strip()])

def main():
    logging.info("🧹 INICIANDO PROTOCOLO DE LIMPIEZA (SISTEMA DE STRIKES)...")
    gc, drive = autenticar_google()
    
    # 1. Obtener la verdad (Excel)
    ids_validos, sh = obtener_ids_validos(gc)
    
    # --- FRENO DE EMERGENCIA ---
    if len(ids_validos) < Config.MINIMO_IDS_SEGURIDAD:
        logging.error(f"⛔ ¡ALERTA! Solo se encontraron {len(ids_validos)} IDs en la hoja.")
        logging.error("   -> Es posible que la hoja se esté actualizando o esté vacía.")
        logging.error("   -> Se ABORTA la limpieza para evitar borrar datos por error.")
        return
    # ---------------------------

    # 2. Obtener la realidad (Drive)
    carpetas_drive = obtener_carpetas_drive(drive)
    
    # 3. Obtener el historial (Log)
    ws_log, ids_en_capilla = gestionar_hoja_log(sh)
    
    logging.info(f"📊 Análisis: {len(ids_validos)} IDs válidos vs {len(carpetas_drive)} carpetas en Drive.")
    
    # Identificar carpetas que sobran
    huerfanos = [nombre for nombre in carpetas_drive if nombre not in ids_validos]
    
    nuevos_en_capilla = [] # Strike 1
    ids_eliminados = []    # Strike 2 (Ya borrados)
    
    if not huerfanos:
        logging.info("✨ Drive está limpio. No sobran carpetas.")
        # Si la lista negra tiene datos pero drive está limpio, limpiamos la lista
        if ids_en_capilla:
            ws_log.clear()
            ws_log.append_row(["ID_MP", "ESTADO"])
        return

    logging.info(f"⚠️ Se detectaron {len(huerfanos)} carpetas sobrantes.")

    # PROCESAMIENTO
    for huerfano in huerfanos:
        if huerfano in ids_en_capilla:
            # --- STRIKE 2: ELIMINACIÓN REAL ---
            folder_id = carpetas_drive[huerfano]
            logging.warning(f"🗑️ [STRIKE 2] Eliminando carpeta confirmada: {huerfano}")
            try:
                drive.files().delete(fileId=folder_id, supportsAllDrives=True).execute()
                ids_eliminados.append(huerfano)
                time.sleep(0.5) 
            except Exception as e:
                logging.error(f"❌ Error borrando {huerfano}: {e}")
        else:
            # --- STRIKE 1: ADVERTENCIA ---
            logging.info(f"👀 [STRIKE 1] Candidato detectado: {huerfano}. Se marcará en la lista.")
            nuevos_en_capilla.append([huerfano, "STRIKE_1"])

    # 4. ACTUALIZACIÓN DEL LOG (MEMORIA)
    
    # A) Agregar los nuevos Strike 1 al final
    if nuevos_en_capilla:
        ws_log.append_rows(nuevos_en_capilla)
        
    # B) Perdonar a los que volvieron a ser válidos
    ids_perdonados = [x for x in ids_en_capilla if x not in huerfanos]
    
    # Si borramos algo o perdonamos algo, hay que reescribir la hoja de log para limpiarla
    if ids_eliminados or ids_perdonados:
        time.sleep(2)
        
        # Re-leemos el log completo actualizado
        lista_actualizada = ws_log.col_values(1)[1:] 
        
        ids_finales = []
        for item in lista_actualizada:
            if item not in ids_eliminados and item not in ids_perdonados:
                ids_finales.append([item, "STRIKE_1"])
        
        # Reescribimos la hoja completa
        ws_log.clear()
        ws_log.append_row(["ID_MP", "ESTADO"])
        if ids_finales:
            ws_log.append_rows(ids_finales)

    if ids_perdonados:
        logging.info(f"🛡️ Se perdonaron {len(ids_perdonados)} carpetas que volvieron a ser válidas.")
    
    logging.info("✅ Proceso de limpieza finalizado.")

if __name__ == "__main__":
    main()
