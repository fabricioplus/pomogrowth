"""Integración con ClickUp para sincronización de tareas."""
import requests
import json
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
import traceback


class ClickUpClient:
    """Cliente para interactuar con la API de ClickUp."""
    
    BASE_URL = "https://api.clickup.com/api/v2"
    
    def __init__(self, api_token: str):
        """
        Inicializa el cliente de ClickUp.
        
        Args:
            api_token: Token de API personal de ClickUp
        """
        self.api_token = api_token
        self.headers = {
            "Authorization": api_token,
            "Content-Type": "application/json"
        }
        self._workspace_cache = {}
        self._space_cache = {}
        self._list_cache = {}
    
    def test_connection(self) -> Tuple[bool, str]:
        """
        Prueba la conexión con ClickUp.
        
        Returns:
            Tupla (success, message)
        """
        try:
            response = requests.get(
                f"{self.BASE_URL}/user",
                headers=self.headers,
                timeout=10
            )
            
            if response.status_code == 200:
                user_data = response.json()
                username = user_data.get('user', {}).get('username', 'Usuario')
                return True, f"✅ Conectado como: {username}"
            elif response.status_code == 401:
                return False, "❌ Token inválido o expirado"
            else:
                return False, f"❌ Error de conexión: {response.status_code}"
        except requests.exceptions.RequestException as e:
            return False, f"❌ Error de red: {str(e)}"
        except Exception as e:
            return False, f"❌ Error inesperado: {str(e)}"
    
    def get_teams(self) -> List[Dict]:
        """
        Obtiene los teams/workspaces disponibles.
        
        Returns:
            Lista de teams
        """
        try:
            response = requests.get(
                f"{self.BASE_URL}/team",
                headers=self.headers,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                teams = data.get('teams', [])
                # Cachear para uso posterior
                for team in teams:
                    self._workspace_cache[team['id']] = team
                return teams
            else:
                print(f"Error obteniendo teams: {response.status_code}")
                return []
        except Exception as e:
            print(f"Error obteniendo teams: {e}")
            return []
    
    def get_spaces(self, team_id: str) -> List[Dict]:
        """
        Obtiene los spaces de un team.
        
        Args:
            team_id: ID del team
            
        Returns:
            Lista de spaces
        """
        try:
            response = requests.get(
                f"{self.BASE_URL}/team/{team_id}/space",
                headers=self.headers,
                params={"archived": "false"},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                spaces = data.get('spaces', [])
                # Cachear
                for space in spaces:
                    self._space_cache[space['id']] = space
                return spaces
            else:
                print(f"Error obteniendo spaces: {response.status_code}")
                return []
        except Exception as e:
            print(f"Error obteniendo spaces: {e}")
            return []
    
    def get_folders(self, space_id: str) -> List[Dict]:
        """
        Obtiene las carpetas de un space.
        
        Args:
            space_id: ID del space
            
        Returns:
            Lista de carpetas
        """
        try:
            response = requests.get(
                f"{self.BASE_URL}/space/{space_id}/folder",
                headers=self.headers,
                params={"archived": "false"},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get('folders', [])
            else:
                print(f"Error obteniendo carpetas: {response.status_code}")
                return []
        except Exception as e:
            print(f"Error obteniendo carpetas: {e}")
            return []
    
    def get_lists(self, folder_id: str = None, space_id: str = None) -> List[Dict]:
        """
        Obtiene las listas de una carpeta o space.
        
        Args:
            folder_id: ID de la carpeta (opcional)
            space_id: ID del space (si no hay folder_id)
            
        Returns:
            Lista de listas
        """
        try:
            if folder_id:
                url = f"{self.BASE_URL}/folder/{folder_id}/list"
            elif space_id:
                url = f"{self.BASE_URL}/space/{space_id}/list"
            else:
                return []
            
            response = requests.get(
                url,
                headers=self.headers,
                params={"archived": "false"},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                lists = data.get('lists', [])
                # Cachear
                for list_item in lists:
                    self._list_cache[list_item['id']] = list_item
                return lists
            else:
                print(f"Error obteniendo listas: {response.status_code}")
                return []
        except Exception as e:
            print(f"Error obteniendo listas: {e}")
            return []
    
    def get_tasks(self, list_id: str, include_completed: bool = False) -> List[Dict]:
        """
        Obtiene las tareas de una lista.
        
        Args:
            list_id: ID de la lista
            include_completed: Si incluir tareas completadas
            
        Returns:
            Lista de tareas
        """
        try:
            params = {
                "archived": "false",
                "include_closed": str(include_completed).lower()
            }
            
            response = requests.get(
                f"{self.BASE_URL}/list/{list_id}/task",
                headers=self.headers,
                params=params,
                timeout=15
            )
            
            if response.status_code == 200:
                data = response.json()
                tasks = data.get('tasks', [])
                
                # Formatear tareas para compatibilidad con PomoGrowth
                formatted_tasks = []
                for task in tasks:
                    formatted_task = self._format_task(task)
                    if formatted_task:
                        formatted_tasks.append(formatted_task)
                
                return formatted_tasks
            else:
                print(f"Error obteniendo tareas: {response.status_code}")
                return []
        except Exception as e:
            print(f"Error obteniendo tareas: {e}")
            traceback.print_exc()
            return []
    
    def get_task(self, task_id: str) -> Optional[Dict]:
        """
        Obtiene una tarea específica.
        
        Args:
            task_id: ID de la tarea
            
        Returns:
            Datos de la tarea o None
        """
        try:
            response = requests.get(
                f"{self.BASE_URL}/task/{task_id}",
                headers=self.headers,
                timeout=10
            )
            
            if response.status_code == 200:
                task = response.json()
                return self._format_task(task)
            else:
                print(f"Error obteniendo tarea: {response.status_code}")
                return None
        except Exception as e:
            print(f"Error obteniendo tarea: {e}")
            return None
    
    def create_task(self, list_id: str, name: str, description: str = "",
                   priority: int = 3, due_date: str = None,
                   assignees: List[str] = None) -> Optional[Dict]:
        """
        Crea una nueva tarea en ClickUp.
        
        Args:
            list_id: ID de la lista
            name: Nombre de la tarea
            description: Descripción (opcional)
            priority: Prioridad 1-4 (1=urgente, 4=baja)
            due_date: Fecha límite en formato ISO
            assignees: Lista de IDs de usuarios asignados
            
        Returns:
            Datos de la tarea creada o None
        """
        try:
            # Sin "status": la lista pone su estado inicial (un "Open" fijo
            # daba 400 en las listas con otros estados)
            payload = {
                "name": name,
                "description": description,
                "priority": priority
            }

            if due_date:
                # Convertir a timestamp en milisegundos
                dt = datetime.fromisoformat(due_date)
                payload["due_date"] = int(dt.timestamp() * 1000)
                # 'YYYY-MM-DD': solo el día, sin hora
                payload["due_date_time"] = len(due_date) > 10
            
            if assignees:
                payload["assignees"] = assignees
            
            response = requests.post(
                f"{self.BASE_URL}/list/{list_id}/task",
                headers=self.headers,
                json=payload,
                timeout=10
            )
            
            if response.status_code == 200:
                task = response.json()
                return self._format_task(task)
            else:
                print(f"Error creando tarea: {response.status_code}")
                print(response.text)
                return None
        except Exception as e:
            print(f"Error creando tarea: {e}")
            return None
    
    def update_task(self, task_id: str, **kwargs) -> bool:
        """
        Actualiza una tarea existente.
        
        Args:
            task_id: ID de la tarea
            **kwargs: Campos a actualizar (name, description, priority, status, etc.)
            
        Returns:
            True si se actualizó exitosamente
        """
        try:
            payload = {}
            
            # Mapear campos
            if 'name' in kwargs:
                payload['name'] = kwargs['name']
            if 'description' in kwargs:
                payload['description'] = kwargs['description']
            if 'priority' in kwargs:
                payload['priority'] = kwargs['priority']
            if 'status' in kwargs:
                payload['status'] = kwargs['status']
            if 'due_date' in kwargs:
                if kwargs['due_date']:
                    dt = datetime.fromisoformat(kwargs['due_date'])
                    payload['due_date'] = int(dt.timestamp() * 1000)
                else:
                    payload['due_date'] = None
            
            response = requests.put(
                f"{self.BASE_URL}/task/{task_id}",
                headers=self.headers,
                json=payload,
                timeout=10
            )
            
            return response.status_code == 200
        except Exception as e:
            print(f"Error actualizando tarea: {e}")
            return False
    
    def complete_task(self, task_id: str) -> bool:
        """
        Marca una tarea como completada.
        
        Args:
            task_id: ID de la tarea
            
        Returns:
            True si se completó exitosamente
        """
        return self.update_task(task_id, status="complete")
    
    def add_comment(self, task_id: str, comment: str) -> bool:
        """
        Añade un comentario a una tarea.
        
        Args:
            task_id: ID de la tarea
            comment: Texto del comentario
            
        Returns:
            True si se añadió exitosamente
        """
        try:
            payload = {
                "comment_text": comment
            }
            
            response = requests.post(
                f"{self.BASE_URL}/task/{task_id}/comment",
                headers=self.headers,
                json=payload,
                timeout=10
            )
            
            return response.status_code == 200
        except Exception as e:
            print(f"Error añadiendo comentario: {e}")
            return False
    
    def delete_task(self, task_id: str) -> bool:
        """
        Elimina una tarea.
        
        Args:
            task_id: ID de la tarea
            
        Returns:
            True si se eliminó exitosamente
        """
        try:
            response = requests.delete(
                f"{self.BASE_URL}/task/{task_id}",
                headers=self.headers,
                timeout=10
            )
            
            return response.status_code == 204
        except Exception as e:
            print(f"Error eliminando tarea: {e}")
            return False
    
    def _format_task(self, task: Dict) -> Dict:
        """
        Formatea una tarea de ClickUp para compatibilidad con PomoGrowth.
        
        Args:
            task: Tarea de ClickUp
            
        Returns:
            Tarea formateada
        """
        try:
            # Extraer campos básicos
            formatted = {
                'clickup_id': task.get('id'),
                'name': task.get('name', 'Sin nombre'),
                'description': task.get('description', ''),
                'source': 'clickup',
                'completed': task.get('status', {}).get('type') == 'closed',
                'tags': [],
                'due': None,
                'priority': None,
                'assignees': []
            }
            
            # Procesar etiquetas
            tags = task.get('tags', [])
            formatted['tags'] = [tag.get('name', '') for tag in tags]
            
            # Procesar fecha límite
            if task.get('due_date'):
                try:
                    # ClickUp envía timestamp en milisegundos
                    timestamp = int(task['due_date']) / 1000
                    formatted['due'] = datetime.fromtimestamp(timestamp).isoformat()
                except Exception:
                    pass
            
            # Procesar prioridad (1=urgente, 2=alta, 3=normal, 4=baja)
            priority_val = task.get('priority')
            if priority_val:
                priority_map = {
                    1: '🔴 Urgente',
                    2: '🟠 Alta',
                    3: '🟡 Normal',
                    4: '🔵 Baja'
                }
                formatted['priority'] = priority_map.get(priority_val.get('priority', 3), '🟡 Normal')
            
            # Procesar asignados
            assignees = task.get('assignees', [])
            formatted['assignees'] = [a.get('username', '') for a in assignees]
            
            # Información adicional de ClickUp
            formatted['clickup_data'] = {
                'list_id': task.get('list', {}).get('id'),
                'list_name': task.get('list', {}).get('name'),
                'folder_id': task.get('folder', {}).get('id'),
                'folder_name': task.get('folder', {}).get('name'),
                'space_id': task.get('space', {}).get('id'),
                'status': task.get('status', {}).get('status', 'Open'),
                'url': task.get('url'),
                'time_estimate': task.get('time_estimate'),
                'time_spent': task.get('time_spent')
            }
            
            return formatted
        except Exception as e:
            print(f"Error formateando tarea: {e}")
            return None
    
    def get_workspaces_and_lists(self) -> List[Dict]:
        """
        Obtiene toda la jerarquía de workspaces, spaces y listas.
        
        Returns:
            Lista de diccionarios con la estructura completa
        """
        hierarchy = []
        
        try:
            teams = self.get_teams()
            
            for team in teams:
                team_data = {
                    'id': team['id'],
                    'name': team['name'],
                    'type': 'team',
                    'spaces': []
                }
                
                spaces = self.get_spaces(team['id'])
                
                for space in spaces:
                    space_data = {
                        'id': space['id'],
                        'name': space['name'],
                        'type': 'space',
                        'lists': []
                    }
                    
                    # Obtener listas directas del space
                    lists = self.get_lists(space_id=space['id'])
                    for list_item in lists:
                        space_data['lists'].append({
                            'id': list_item['id'],
                            'name': list_item['name'],
                            'type': 'list'
                        })
                    
                    # Obtener carpetas y sus listas
                    folders = self.get_folders(space['id'])
                    for folder in folders:
                        folder_lists = self.get_lists(folder_id=folder['id'])
                        for list_item in folder_lists:
                            space_data['lists'].append({
                                'id': list_item['id'],
                                'name': f"{folder['name']}/{list_item['name']}",
                                'type': 'list',
                                'folder': folder['name']
                            })
                    
                    if space_data['lists']:
                        team_data['spaces'].append(space_data)
                
                if team_data['spaces']:
                    hierarchy.append(team_data)
            
            return hierarchy
        except Exception as e:
            print(f"Error obteniendo jerarquía: {e}")
            return []
