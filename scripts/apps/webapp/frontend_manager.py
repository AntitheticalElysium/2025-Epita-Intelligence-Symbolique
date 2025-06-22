#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Frontend Manager - Gestionnaire du frontend React (optionnel)
=============================================================

Gère le démarrage et l'arrêt du frontend React quand nécessaire.

Auteur: Projet Intelligence Symbolique EPITA
Date: 07/06/2025
"""

import os
import sys
import time
import asyncio
import logging
import subprocess
import re
from typing import Dict, Optional, Any, Tuple
from pathlib import Path
import aiohttp
import psutil


class FrontendManager:
    """
    Gestionnaire du frontend React
    
    Fonctionnalités :
    - Démarrage serveur de développement React
    - Installation dépendances automatique
    - Health check de l'interface
    - Arrêt propre
    - Création de build pour la production
    """

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        self.config = config
        self.logger = logger
        
        # NOTE DE FUSION: On combine la recherche de chemin du stash et la gestion de port de l'upstream.
        self.enabled = config.get('enabled', True)
        self.frontend_path = self._find_frontend_path(config.get('path'))
        self.start_port = config.get('start_port', 3000)
        self.fallback_ports = config.get('fallback_ports', [3001, 3002])
        self.start_command = config.get('start_command', 'npm start')
        self.build_command = config.get('build_command', 'npm run build')
        self.timeout_seconds = config.get('timeout_seconds', 90)
        self.max_attempts = config.get('max_attempts', 5)
        
        # État runtime
        self.process: Optional[subprocess.Popen] = None
        self.current_port: Optional[int] = None
        self.current_url: Optional[str] = None
        self.pid: Optional[int] = None
        self.frontend_stdout_log_file: Optional[Any] = None
        self.frontend_stderr_log_file: Optional[Any] = None

    # NOTE DE FUSION: On garde la logique de recherche de `_find_frontend_path`
    # tout en intégrant la logique de démarrage robuste de l'upstream.
    def _find_frontend_path(self, configured_path: Optional[str]) -> Optional[Path]:
        """Trouve le chemin du projet frontend de manière robuste."""
        if configured_path and Path(configured_path).exists():
            self.logger.info(f"Utilisation du chemin frontend configuré : {configured_path}")
            return Path(configured_path)

        project_root = Path.cwd()
        candidate_paths = [
            project_root / "interface_web",
            project_root / "frontend",
            project_root / "services/web_api/interface-web-argumentative"
        ]
        for path in candidate_paths:
            if (path / "package.json").exists():
                self.logger.info(f"Chemin frontend auto-détecté : {path}")
                return path
        
        self.logger.error("Impossible de trouver le répertoire du projet frontend : vérifiez la config ou la structure des dossiers.")
        return None

    async def start_with_failover(self) -> Dict[str, Any]:
        """
        Démarre le frontend avec failover si le port est occupé.
        """
        if not self.enabled:
            return {'success': True, 'error': 'Frontend désactivé'}

        if not self.frontend_path:
            return {'success': False, 'error': 'Chemin du frontend non trouvé.'}

        ports_to_try = [self.start_port] + self.fallback_ports
        
        for port in ports_to_try:
            self.logger.info(f"Tentative de démarrage du Frontend sur le port {port}")
            if await self._is_port_occupied(port):
                self.logger.warning(f"Le port {port} est déjà occupé.")
                continue

            result = await self._start_on_port(port)
            if result['success']:
                return result
        
        error_msg = f"Impossible de démarrer le frontend sur les ports configurés: {ports_to_try}"
        self.logger.error(error_msg)
        return {'success': False, 'error': error_msg}

    async def _start_on_port(self, port: int) -> Dict[str, Any]:
        """
        Tente de démarrer le serveur de développement sur un port donné.
        """
        if not await self._ensure_dependencies():
            return {'success': False, 'error': "Échec de l'installation des dépendances npm"}

        try:
            self.logger.info(f"Exécution de la commande de démarrage: {self.start_command}")
            cmd = ['cmd', '/c'] + self.start_command.split() if sys.platform == "win32" else ['sh', '-c', self.start_command]
            
            log_dir = Path("logs"); log_dir.mkdir(exist_ok=True)
            self.frontend_stdout_log_file = open(log_dir / "frontend_stdout.log", "wb")
            self.frontend_stderr_log_file = open(log_dir / "frontend_stderr.log", "wb")
            
            self.process = subprocess.Popen(
                cmd,
                stdout=self.frontend_stdout_log_file,
                stderr=self.frontend_stderr_log_file,
                cwd=self.frontend_path,
                env=self._get_frontend_env(port),
            )
            
            frontend_ready, final_port, final_url = await self._wait_for_frontend(port)

            if frontend_ready:
                self.current_port = final_port
                self.current_url = final_url
                self.pid = self.process.pid
                self.logger.info(f"Frontend démarré avec succès. PID: {self.pid}, URL: {self.current_url}")
                return {'success': True, 'url': self.current_url, 'port': self.current_port, 'pid': self.pid}
            else:
                await self.stop()
                return {'success': False, 'error': f"Échec du démarrage du frontend sur le port {port}"}

        except Exception as e:
            self.logger.critical(f"Erreur critique lors du démarrage du frontend sur le port {port}: {e}", exc_info=True)
            await self.stop()
            return {'success': False, 'error': str(e)}
    
    async def _ensure_dependencies(self):
        """S'assure que les dépendances npm sont installées"""
        node_modules = self.frontend_path / 'node_modules'
        
        if not node_modules.exists():
            self.logger.info("Installation dépendances npm...")
            
            try:
                process = subprocess.Popen(
                    ['npm', 'install'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.frontend_path
                )
                
                stdout, stderr = process.communicate(timeout=120)  # 2 min max
                
                if process.returncode != 0:
                    self.logger.error(f"Échec npm install: {stderr.decode()}")
                else:
                    self.logger.info("Dépendances npm installées")
                    
            except subprocess.TimeoutExpired:
                process.kill()
                self.logger.error("Timeout installation npm")
            except Exception as e:
                self.logger.error(f"Erreur npm install: {e}")
    
    def _get_frontend_env(self, port: int) -> Dict[str, str]:
        """Prépare l'environnement pour le frontend avec un port dynamique."""
        env = os.environ.copy()
        env.update({
            'BROWSER': 'none',
            'PORT': str(port),
            'GENERATE_SOURCEMAP': 'false',
            'SKIP_PREFLIGHT_CHECK': 'true'
        })
        return env

    async def _wait_for_frontend(self, initial_port: int) -> Tuple[bool, Optional[int], Optional[str]]:
        """Attend que le frontend soit accessible."""
        start_time = time.time()
        end_time = start_time + self.timeout_seconds
        
        detected_port = initial_port
        
        while time.time() < end_time:
            # Vérifier si le processus est toujours actif
            if self.process.poll() is not None:
                self.logger.error(f"Processus frontend terminé prématurément (code: {self.process.returncode}).")
                return False, None, None

            # Tentative de détection du port depuis les logs (Create React App peut changer de port)
            log_file = Path("logs/frontend_stdout.log")
            if log_file.exists():
                try:
                    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                        log_content = f.read()
                    match = re.search(r"Local:\s+(http://localhost:(\d+))", log_content)
                    if match:
                        new_port = int(match.group(2))
                        if new_port != detected_port:
                            self.logger.info(f"React a changé de port: {detected_port} -> {new_port}")
                            detected_port = new_port
                except Exception:
                    pass

            url_to_check = f"http://localhost:{detected_port}"
            try:
                async with aiohttp.ClientSession() as session:
                    self.logger.info(f"Tentative de connexion à {url_to_check}...")
                    async with session.get(url_to_check, timeout=15) as response:
                        self.logger.info(f"Réponse reçue de {url_to_check} avec statut: {response.status}")
                        if response.status == 200:
                            self.logger.info(f"🎉 Frontend accessible sur {url_to_check} après {time.time() - start_time:.1f}s.")
                            return True, detected_port, url_to_check
            except aiohttp.ClientError as e:
                self.logger.warning(f"Échec de connexion à {url_to_check}: {e}")
                pass # On continue d'attendre

            await asyncio.sleep(2)

        self.logger.error(f"Timeout - Frontend non accessible sur {url_to_check} après {self.timeout_seconds}s")
        return False, None, None

    async def _is_port_occupied(self, port: int) -> bool:
        """Vérifie si un port est déjà occupé."""
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                    return True
        except (psutil.AccessDenied, AttributeError):
            # Fallback pour les systèmes où psutil a des limitations
            try:
                with aiohttp.ClientSession() as session:
                    async with session.get(f"http://127.0.0.1:{port}", timeout=1):
                        return True
            except:
                pass
        return False

    async def health_check(self) -> bool:
        """Vérifie l'état de santé du frontend"""
        if not self.current_url:
            return False
            
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.current_url, 
                                     timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        self.logger.info("Frontend health OK")
                        return True
        except Exception as e:
            self.logger.error(f"Frontend health check échec: {e}")
            
        return False
    
    async def stop(self):
        """Arrête le frontend proprement"""
        if self.process:
            try:
                self.logger.info(f"Arrêt frontend PID {self.process.pid}")
                
                # Terminaison progressive
                self.process.terminate()
                
                # Attente arrêt propre (10s max pour React)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # Force kill si nécessaire
                    self.process.kill()
                    self.process.wait()
                    
                self.logger.info("Frontend arrêté")
                
            except Exception as e:
                self.logger.error(f"Erreur arrêt frontend: {e}")
            finally:
                if self.frontend_stdout_log_file:
                    try:
                        self.frontend_stdout_log_file.close()
                    except Exception as log_e:
                        self.logger.error(f"Erreur fermeture frontend_stdout_log_file: {log_e}")
                    self.frontend_stdout_log_file = None
                
                if self.frontend_stderr_log_file:
                    try:
                        self.frontend_stderr_log_file.close()
                    except Exception as log_e:
                        self.logger.error(f"Erreur fermeture frontend_stderr_log_file: {log_e}")
                    self.frontend_stderr_log_file = None

                self.process = None
                self.current_url = None
                self.pid = None
    
    def get_status(self) -> Dict[str, Any]:
        """Retourne l'état actuel du frontend"""
        return {
            'enabled': self.enabled,
            'running': self.process is not None,
            'port': self.current_port,
            'url': self.current_url,
            'pid': self.pid,
            'path': str(self.frontend_path),
            'process': self.process
        }
if __name__ == '__main__':
    """
    Point d'entrée pour l'exécution directe.
    Exemple:
    python scripts/apps/webapp/frontend_manager.py build
    python scripts/apps/webapp/frontend_manager.py start
    """
    import argparse
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s - %(message)s')
    logger = logging.getLogger("FrontendManagerCLI")

    parser = argparse.ArgumentParser(description="Gestionnaire de build et de serveur de développement Frontend.")
    parser.add_argument('action', choices=['build', 'start'], help="L'action à effectuer: 'build' pour créer les fichiers statiques, 'start' pour lancer le serveur de dev.")
    args = parser.parse_args()

    manager = FrontendManager(config={}, logger=logger)

    async def main():
        if not manager.enabled or not manager.frontend_path:
            logger.error("Frontend Manager n'est pas activé ou le chemin est introuvable. Arrêt.")
            sys.exit(1)

        if args.action == 'build':
            logger.info("--- Démarrage du Build Frontend ---")
            success = await manager.build()
            if success:
                logger.info("--- Build terminé avec succès ---")
                sys.exit(0)
            else:
                logger.error("--- Le Build a échoué ---")
                sys.exit(1)
        
        elif args.action == 'start':
            logger.info("--- Démarrage du Serveur de Développement Frontend ---")
            result = await manager.start_dev_server()
            if result.get('success'):
                logger.info(f"Serveur démarré avec succès sur {result.get('url')}")
                # Le script reste en cours d'exécution car le serveur est un processus de longue durée
                try:
                    while True:
                        await asyncio.sleep(60)
                except KeyboardInterrupt:
                    logger.info("Arrêt manuel du serveur...")
                    await manager.stop()
                    logger.info("Serveur arrêté.")
            else:
                logger.error(f"--- Échec du démarrage du serveur de développement: {result.get('error')} ---")
                sys.exit(1)

    asyncio.run(main())