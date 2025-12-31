import os
import time
import shutil
import logging
import gspread
import gc
import random
import argparse
import sys
import json
import re  # Para sanitizar nombres
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import UnexpectedAlertPresentException, TimeoutException

# NOTA: Se eliminó 'webdriver_manager' porque usaremos el del sistema (ARM64)

# --- CONFIGURACIÓN CENTRALIZADA ---
class Config:
    # Credenciales y IDs (AHORA DESDE VARIABLES DE ENTORNO)
    ID_HOJA_CALCULO = os.environ.get("SHEET_ID")
    ID_CARPETA_DRIVE_DESTINO = os.environ.get("DRIVE_FOLDER_ID")
    
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

    # Columnas de la Hoja de Cálculo (basado en 1)
    COLUMNA_URL = 1
    COLUMNA_ID = 2
    COLUMNA_ENLACE = 15
    COLUMNA_PRIORIDAD = 16

    # Parámetros de Ejecución
    TAMANO_LOTE = 25
    REINTENTOS_PROCESO = 1

    # Configuración de Selenium
    SELENIUM_TIMEOUT = 20 
    PAGE_LOAD_TIMEOUT = 60

    # Inicializar Logging Globalmente
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')

    @staticmethod
    def get_temp_folder(lote_num=None):
        """Genera una ruta de carpeta temporal única por lote."""
        folder_name = "temp_descargas"
        if lote_num:
            folder_name = f"temp_descargas_{lote_num}"
        return os.path.join(os.getcwd(), folder_name)

# --- UTILS DE SEGURIDAD ---
def limpiar_nombre_archivo(nombre):
    """Elimina caracteres peligrosos para el sistema de archivos."""
    if not nombre: return ""
    # Solo permite letras, números, guiones, puntos y espacios
    return re.sub(r'[^\w\-. ]', '', str(nombre))

# --- INICIALIZACIÓN DE ARGUMENTOS ---
parser = argparse.ArgumentParser(description='Bot Licitaciones Paralelo')
parser.add_argument('--lote', type=int, default=1, help='Número del clon actual (ej: 1)')
parser.add_argument('--total_lotes', type=int, default=1, help='Total de clones corriendo (ej: 2)')
ARGS, unknown = parser.parse_known_args()

CARPETA_TEMP = Config.get_temp_folder(ARGS.lote if ARGS.total_lotes > 1 else None)

# --- CONEXIÓN ---

def autenticar_google():
    logging.info("🔑 Conectando con Google (Modo Seguro)...")
    
    # Validaciones Previas
    if not Config.ID_HOJA_CALCULO:
        logging.error("❌ FALTA CONFIGURACIÓN: Variable 'SHEET_ID' no encontrada.")
        sys.exit(1)
    if not Config.ID_CARPETA_DRIVE_DESTINO:
        logging.error("❌ FALTA CONFIGURACIÓN: Variable 'DRIVE_FOLDER_ID' no encontrada.")
        sys.exit(1)

    json_creds = os.environ.get("GCP_CREDENTIALS")
    if not json_creds:
        logging.error("❌ ERROR CRÍTICO: No se encontró la variable de entorno 'GCP_CREDENTIALS'")
        sys.exit(1)
        
    try:
        creds_dict = json.loads(json_creds)
        credenciales = Credentials.from_service_account_info(creds_dict, scopes=Config.SCOPES)
        cliente_gc = gspread.authorize(credenciales)
        servicio_drive = build('drive', 'v3', credentials=credenciales)
        return cliente_gc, servicio_drive
    except json.JSONDecodeError:
        logging.error("❌ ERROR: El secreto GCP_CREDENTIALS no es un JSON válido.")
        sys.exit(1)
    except Exception as e:
        logging.error(f"❌ Error al autenticar: {e}")
        sys.exit(1)

def iniciar_navegador():
    if not os.path.exists(CARPETA_TEMP): os.makedirs(CARPETA_TEMP)
    for f in os.listdir(CARPETA_TEMP):
        try: os.remove(os.path.join(CARPETA_TEMP, f))
        except OSError as e: logging.warning(f"No se pudo borrar el archivo temporal {f}: {e}")

    opciones = webdriver.ChromeOptions()
    
    # --- CONFIGURACIÓN CRÍTICA PARA ARM64 (CELULAR / PPA XTRADEB) ---
    # En el paquete xtradeb, el binario suele llamarse 'chromium' a secas
    opciones.binary_location = "/usr/bin/chromium"

    # --- OPTIMIZACIÓN Y DESCARGAS ---
    preferencias = {
        "download.default_directory": os.path.abspath(CARPETA_TEMP),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True, 
        "profile.default_content_setting_values.automatic_downloads": 1,
        "safebrowsing.enabled": True,
        "profile.content_settings.exceptions.popups": 1,
        "profile.managed_default_content_settings.images": 1, 
        "profile.managed_default_content_settings.stylesheets": 1,
    }
    opciones.add_experimental_option("prefs", preferencias)
    opciones.page_load_strategy = 'normal' 
    
    # --- MODO FURTIVO ---
    opciones.add_argument("--disable-blink-features=AutomationControlled") 
    opciones.add_experimental_option("excludeSwitches", ["enable-automation"]) 
    opciones.add_experimental_option('useAutomationExtension', False) 

    # --- CONFIGURACIÓN PARA SERVIDOR (GITHUB ACTIONS / TERMUX) ---
    opciones.add_argument("--headless=new") 
    opciones.add_argument("--window-size=1920,1080")
    opciones.add_argument("--disable-gpu")
    
    # CRITICO PARA ROOT EN TERMUX
    opciones.add_argument("--no-sandbox")
    opciones.add_argument("--disable-dev-shm-usage")
    
    # --- PARCHES EXTRA PARA ESTABILIDAD (EVITA ERROR STATUS 1) ---
    opciones.add_argument("--remote-debugging-port=9222")
    opciones.add_argument("--disable-software-rasterizer")

    # Optimización de caché para evitar llenado de disco en Actions
    opciones.add_argument("--disk-cache-dir=/dev/null") 
    opciones.add_argument("--disk-cache-size=1")
    
    opciones.add_argument("--log-level=3") 
    opciones.add_argument("user-agent=Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    
    # --- INICIO DEL DRIVER SIN WEBDRIVER-MANAGER ---
    # Usamos la ruta directa donde apt instala el driver
    driver_path = "/usr/bin/chromedriver"
    
    try:
        service = Service(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=opciones)
        
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        driver.set_page_load_timeout(Config.PAGE_LOAD_TIMEOUT)
        return driver
    except Exception as e:
        logging.error(f"❌ Error iniciando WebDriver: {e}")
        logging.error("Asegúrate de haber instalado los paquetes del PPA xtradeb/apps correctamente.")
        raise e

# --- DRIVE ---

def obtener_nombres_carpetas_existentes(drive_service):
    logging.info("🔍 Escaneando Drive para caché de carpetas...")
    nombres_existentes = set()
    page_token = None
    try:
        while True:
            q = f"'{Config.ID_CARPETA_DRIVE_DESTINO}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            response = drive_service.files().list(q=q, fields='nextPageToken, files(name)', pageToken=page_token, supportsAllDrives=True, includeItemsFromAllDrives=True, pageSize=1000).execute()
            for file in response.get('files', []):
                nombres_existentes.add(file.get('name'))
            page_token = response.get('nextPageToken', None)
            if page_token is None: break
    except Exception as e:
        logging.error(f"⚠️ Error al escanear carpetas de Drive: {e}")
    return nombres_existentes

def obtener_o_crear_carpeta_destino(drive_service, id_mp, id_padre):
    try:
        q = f"'{id_padre}' in parents and name = '{id_mp}' and trashed = false"
        res = drive_service.files().list(q=q, fields="files(id, webViewLink)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files = res.get('files', [])
        
        if files: 
            return files[0]['id'], files[0].get('webViewLink'), False 
        else:
            metadata = {'name': id_mp, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [id_padre]}
            file = drive_service.files().create(body=metadata, fields='id, webViewLink', supportsAllDrives=True).execute()
            try: drive_service.permissions().create(fileId=file.get('id'), body={'type': 'anyone', 'role': 'reader'}, supportsAllDrives=True).execute()
            except Exception as e: logging.warning(f"No se pudo hacer pública la carpeta {id_mp}: {e}")
            return file.get('id'), file.get('webViewLink'), True 
    except Exception as e:
        logging.error(f"No se pudo obtener o crear la carpeta de Drive para {id_mp}: {e}")
        return None, None, False

def subir_archivo_rapido(drive_service, ruta_local, metadatos):
    try:
        media = MediaFileUpload(ruta_local, resumable=True)
        drive_service.files().create(body=metadatos, media_body=media, fields='id', supportsAllDrives=True).execute()
        return True
    except Exception as e1:
        logging.warning(f"Primer intento de subida falló para {ruta_local}. Reintentando... Error: {e1}")
        time.sleep(2)
        try:
            media = MediaFileUpload(ruta_local, resumable=True)
            drive_service.files().create(body=metadatos, media_body=media, fields='id', supportsAllDrives=True).execute()
            return True
        except Exception as e2:
            logging.error(f"Falló la subida de {ruta_local} en el reintento. Error: {e2}")
            return False

def limpiar_carpetas_obsoletas(drive_service, ids_excel_validos):
    pass 

# --- UTILIDADES ---

def espera_humana(min_seg=2.0, max_seg=4.0):
    tiempo = random.uniform(min_seg, max_seg)
    time.sleep(tiempo)

def esperar_nuevo_archivo(carpeta, cantidad_antes, timeout=20): 
    inicio = time.time() 
    while time.time() - inicio < timeout:
        actuales = len(os.listdir(carpeta))
        if actuales > cantidad_antes:
            return True
        time.sleep(0.3)
    return False

def esperar_fin_todas_descargas(carpeta, timeout=120): 
    inicio = time.time()
    while time.time() - inicio < timeout:
        archivos = os.listdir(carpeta)
        if not archivos: return True 
        descargando = [f for f in archivos if f.endswith('.crdownload') or f.endswith('.tmp')]
        if not descargando:
            time.sleep(1)
            if not [f for f in os.listdir(carpeta) if f.endswith('.crdownload') or f.endswith('.tmp')]:
                return True
        time.sleep(1)
    return False

def escribir_enlace_seguro(worksheet, id_mp, link_carpeta):
    try:
        celda = worksheet.find(id_mp, in_column=Config.COLUMNA_ID)
        if celda:
            worksheet.update_cell(celda.row, Config.COLUMNA_ENLACE, link_carpeta)
            return True
    except Exception as e:
        logging.error(f"Error al escribir enlace para {id_mp} en la hoja: {e}")
    return False

def actualizar_prioridad(worksheet, id_mp, valor):
    logging.info(f"   -> [Sheet] Actualizando '{id_mp}' a '{valor}'...")
    try:
        celda = worksheet.find(id_mp, in_column=Config.COLUMNA_ID)
        if not celda:
            celda = worksheet.find(id_mp.strip(), in_column=Config.COLUMNA_ID)

        if celda:
            worksheet.update_cell(celda.row, Config.COLUMNA_PRIORIDAD, valor)
            logging.info(f"      ✅ ÉXITO (Fila {celda.row} actualizada).")
            return True
        else:
            logging.error("      ❌ ERROR: ¡ID no encontrado en el Excel!")
            return False
    except Exception as e:
        logging.error(f"      ❌ ERROR API CRÍTICO: {e}")
    return False

# --- MANEJO DE ALERTAS ---
def manejar_alertas(driver):
    try:
        WebDriverWait(driver, 0.5).until(EC.alert_is_present())
        alerta = driver.switch_to.alert
        logging.warning(f"Alerta de navegador detectada y aceptada: {alerta.text}")
        alerta.accept()
        return True
    except TimeoutException:
        return False
    except:
        return False

# --- LÓGICA PRINCIPAL ---

def obtener_datos_licitaciones(gc_client):
    logging.info("📊 Leyendo datos de Google Sheet...")
    sh = gc_client.open_by_key(Config.ID_HOJA_CALCULO)
    worksheet = sh.get_worksheet(0) 
    datos = worksheet.get_all_values()
    lista = []
    ids_validos = set()
    
    for i, fila in enumerate(datos[1:]): 
        if len(fila) >= Config.COLUMNA_ID and fila[Config.COLUMNA_URL - 1] and fila[Config.COLUMNA_ID - 1]:
            id_limpio = fila[Config.COLUMNA_ID - 1].strip()
            prioridad = fila[Config.COLUMNA_PRIORIDAD - 1].strip() if len(fila) >= Config.COLUMNA_PRIORIDAD else ""
            lista.append({
                "url_ficha": fila[Config.COLUMNA_URL - 1].strip(),
                "id_mp": id_limpio,
                "prioridad": prioridad
            })
            ids_validos.add(id_limpio)
            
    logging.info(f"Se encontraron {len(lista)} licitaciones en la hoja.")
    return lista, ids_validos, worksheet

def procesar_lote(lote_datos, drive_service, worksheet):
    for licitacion in lote_datos:
        id_mp = licitacion['id_mp']
        logging.info(f"🔵 [{id_mp}] Iniciando proceso...")
        
        intentos_max = Config.REINTENTOS_PROCESO
        exito = False

        for intento in range(intentos_max):
            driver = None
            try:
                driver = iniciar_navegador()
            except Exception as e:
                logging.error(f"[{id_mp}] 💥 Error fatal al iniciar navegador: {e}")
                continue 

            try:
                # --- PREPARACIÓN CARPETAS ---
                id_carpeta_destino, link_carpeta, es_nueva = obtener_o_crear_carpeta_destino(drive_service, id_mp, Config.ID_CARPETA_DRIVE_DESTINO)
                if link_carpeta: escribir_enlace_seguro(worksheet, id_mp, link_carpeta)

                mapa_archivos_drive = {}
                if id_carpeta_destino and not es_nueva:
                      res = drive_service.files().list(q=f"'{id_carpeta_destino}' in parents and trashed=false", fields="files(id, name)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
                      for f in res.get('files', []): mapa_archivos_drive[f['name'].lower()] = f['id']

                ventana_principal = driver.current_window_handle
                
                # --- NAVEGACIÓN ---
                logging.info(f"   -> 🌍 Navegando a ficha...")
                try:
                    driver.get(licitacion['url_ficha'])
                    espera_humana(2, 4)

                    if "forbidden" in driver.title.lower() or "access denied" in driver.page_source.lower():
                        raise Exception("Bloqueo 403 en Ficha Principal")

                    manejar_alertas(driver)
                    espera = WebDriverWait(driver, Config.SELENIUM_TIMEOUT)
                    espera_humana(1, 2)
                    
                    try:
                        btn_adj = espera.until(EC.element_to_be_clickable((By.ID, "imgAdjuntos")))
                        btn_adj.click()
                        
                        espera.until(EC.number_of_windows_to_be(2))
                        ventanas = driver.window_handles
                        driver.switch_to.window([v for v in ventanas if v != ventana_principal][0])
                        
                        espera_humana(3, 5) 

                        if "forbidden" in driver.title.lower() or "access denied" in driver.page_source.lower():
                            raise Exception("Bloqueo 403 en Popup")

                    except UnexpectedAlertPresentException:
                        manejar_alertas(driver)
                        logging.warning("   -> ⚠️ Alerta web detectada")
                        raise 
                    except Exception as e:
                        if len(driver.window_handles) > 1: driver.close(); driver.switch_to.window(ventana_principal)
                        logging.error(f"   -> ❌ Error abriendo adjuntos: {e}")
                        raise 

                    xpath_btns = "//input[contains(@id, 'DWNL_grdId') and @type='image']"
                    try:
                        espera.until(EC.presence_of_element_located((By.XPATH, xpath_btns)))
                        btns = [e for e in driver.find_elements(By.XPATH, xpath_btns) if e.is_displayed()]
                        if not btns: raise Exception("Sin botones")
                    except:
                        logging.warning("   -> Ø No se encontraron botones (Vacío/Timeout)")
                        driver.close(); driver.switch_to.window(ventana_principal)
                        raise 

                    for f in os.listdir(CARPETA_TEMP):
                        try: os.remove(os.path.join(CARPETA_TEMP, f))
                        except: pass
                    
                    cola = []
                    botones_a_clic = []

                    # 1. SELECCIÓN
                    for btn in btns:
                        desc_limpia = ""
                        try:
                            celdas = btn.find_element(By.XPATH, "./ancestor::tr").find_elements(By.TAG_NAME, "td")
                            if len(celdas) >= 5: 
                                txt = celdas[4].text.strip()
                                # USO DE LA FUNCIÓN DE SANITIZACIÓN SEGURA
                                desc_limpia = limpiar_nombre_archivo(txt)[:80]
                        except: pass

                        if desc_limpia and len(desc_limpia) > 3:
                            if any(desc_limpia.lower() in nom for nom in mapa_archivos_drive): continue 
                        
                        botones_a_clic.append((btn, desc_limpia))

                    if not botones_a_clic:
                        logging.info("   -> ✅ Sin archivos nuevos que descargar.")
                        driver.close(); driver.switch_to.window(ventana_principal)
                        exito = True; break

                    logging.info(f"   -> ⬇️ Descargando {len(botones_a_clic)} archivos...")

                    archivos_antes_del_loop = 0 
                    contador_saturacion = 0 
                    
                    # 2. DESCARGA
                    for btn, desc in botones_a_clic:
                        if contador_saturacion > 0 and contador_saturacion % 5 == 0:
                            wait_long = random.randint(10, 15)
                            time.sleep(wait_long)
                        
                        contador_saturacion += 1
                        
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                        espera_humana(2.0, 4.0)
                        
                        try: btn.click()
                        except: 
                            try: ActionChains(driver).move_to_element(btn).click().perform()
                            except: pass
                        
                        if manejar_alertas(driver): continue

                        if esperar_nuevo_archivo(CARPETA_TEMP, archivos_antes_del_loop, timeout=25):
                            time.sleep(1.0) 
                            archivos_ahora = os.listdir(CARPETA_TEMP)
                            archivos_en_cola = [c['temp'] for c in cola]
                            nuevo_nombre = next((f for f in archivos_ahora if f not in archivos_en_cola), None)
                            if nuevo_nombre:
                                cola.append({"temp": nuevo_nombre, "desc": desc})
                                archivos_antes_del_loop += 1
                            else: logging.warning("      ? Archivo fantasma")
                        else: logging.warning("      x Falló descarga de un archivo")
                        
                    # 3. SUBIDA
                    esperar_fin_todas_descargas(CARPETA_TEMP, timeout=120)

                    intentos_extra = 0
                    while len(os.listdir(CARPETA_TEMP)) < len(botones_a_clic) and intentos_extra < 3:
                        time.sleep(1); intentos_extra += 1
                    
                    logging.info("   -> ☁️ Subiendo a Drive...")

                    archivos_disco = set(os.listdir(CARPETA_TEMP))
                    
                    for item in cola:
                        base = item['temp'].replace('.crdownload', '').replace('.tmp', '')
                        real = next((f for f in archivos_disco if f.startswith(base)), None)
                        
                        if real:
                            if real in archivos_disco: archivos_disco.remove(real)
                            # NOMBRE FINAL SANITIZADO Y SEGURO
                            nombre_final = f"{os.path.splitext(real)[0]}__{item['desc']}{os.path.splitext(real)[1]}" if item['desc'] else real

                            if nombre_final.lower() in mapa_archivos_drive: continue

                            ruta_final = os.path.join(CARPETA_TEMP, nombre_final)
                            try:
                                shutil.move(os.path.join(CARPETA_TEMP, real), ruta_final)
                                subir_archivo_rapido(drive_service, ruta_final, {'name': nombre_final, 'parents': [id_carpeta_destino]})
                            except: pass
                    
                    driver.close(); driver.switch_to.window(ventana_principal)
                    logging.info("   -> ✨ CICLO COMPLETADO EXITOSAMENTE")
                    exito = True
                    break 

                except Exception as e:
                    logging.error(f"   -> ⚠️ ERROR EN EL PROCESO: {e}")
                    if intento < intentos_max - 1:
                        wait = random.randint(45, 90)
                        logging.info(f"   -> Esperando {wait}s para reintentar...")
                        time.sleep(wait)
                        continue 
                    else:
                        logging.error("   -> 💀 FALLO FINAL. Se mantiene Prioridad 1.")
                        actualizar_prioridad(worksheet, id_mp, "1")

            finally:
                if driver:
                    try: driver.quit()
                    except: pass
                if os.path.exists(CARPETA_TEMP):
                    try: shutil.rmtree(CARPETA_TEMP, ignore_errors=True)
                    except: pass
        
        if exito:
            actualizar_prioridad(worksheet, id_mp, "")
            
def filtrar_datos_para_lote(lista_completa, indice_lote, total_lotes):
    if not lista_completa: return []
    sub_lista = [item for i, item in enumerate(lista_completa) if i % total_lotes == (indice_lote - 1)]
    return sub_lista

def main():
    mi_lote = ARGS.lote
    total_bots = ARGS.total_lotes

    print(f"\n⏳ [Bot {mi_lote}] INICIANDO...")
    
    gc_client, drive_service = autenticar_google()
    datos, ids_validos, worksheet = obtener_datos_licitaciones(gc_client)
    carpetas_drive = obtener_nombres_carpetas_existentes(drive_service)

    nuevos_total = [d for d in datos if d['id_mp'] not in carpetas_drive]
    existentes_todos = [d for d in datos if d['id_mp'] in carpetas_drive]
    prioritarios_total = [d for d in existentes_todos if d.get("prioridad") == "1"]
    existentes_normales_total = [d for d in existentes_todos if d.get("prioridad") != "1"]

    mis_nuevos = filtrar_datos_para_lote(nuevos_total, mi_lote, total_bots)
    mis_prioritarios = filtrar_datos_para_lote(prioritarios_total, mi_lote, total_bots)
    mis_existentes = filtrar_datos_para_lote(existentes_normales_total, mi_lote, total_bots)

    print(f"📊 RESUMEN DE TRABAJO:")
    print(f"   - Nuevos:       {len(mis_nuevos)}")
    print(f"   - Prioritarios: {len(mis_prioritarios)}")
    
    if mis_nuevos:
        print(f"\n🚀 PROCESANDO NUEVOS...")
        for i in range(0, len(mis_nuevos), Config.TAMANO_LOTE):
            procesar_lote(mis_nuevos[i:i+Config.TAMANO_LOTE], drive_service, worksheet)
            gc.collect()
    
    if mis_prioritarios:
        print(f"\n🔥 PROCESANDO PRIORITARIOS...")
        for i in range(0, len(mis_prioritarios), Config.TAMANO_LOTE):
            procesar_lote(mis_prioritarios[i:i+Config.TAMANO_LOTE], drive_service, worksheet)
            gc.collect()

    # Si hay existentes y quieres revisarlos, descomenta esto (consume mucho tiempo)
    # if mis_existentes: ...

    if mi_lote == 1:
        logging.info("\n[Bot 1] Ejecutando limpieza final...")
        limpiar_carpetas_obsoletas(drive_service, ids_validos)

    print(f"\n✅ TERMINADO TOTAL.")

if __name__ == "__main__":
    main()
