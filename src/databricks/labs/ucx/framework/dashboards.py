import dataclasses
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import DatabricksError
from databricks.sdk.service.sql import (
    AccessControl,
    ObjectTypePlural,
    PermissionLevel,
    RunAsRole,
    WidgetOptions,
    WidgetPosition,
)
from databricks.sdk.service.workspace import ImportFormat

logger = logging.getLogger(__name__)


@dataclass
class SimpleQuery:
    dashboard_ref: str
    name: str
    query: str
    viz: dict[str, str]
    widget: dict[str, str]

    @property
    def query_key(self):
        return f"{self.dashboard_ref}_{self.name}:query_id"

    @property
    def viz_key(self):
        return f"{self.dashboard_ref}_{self.name}:viz_id"

    @property
    def widget_key(self):
        return f"{self.dashboard_ref}_{self.name}:widget_id"

    @property
    def viz_type(self) -> str:
        return self.viz.get("type", None)

    @property
    def viz_args(self) -> dict:
        return {k: v for k, v in self.viz.items() if k not in ["type"]}


@dataclass
class VizColumn:
    name: str
    title: str
    type: str = "string"  # noqa: A003
    imageUrlTemplate: str = "{{ @ }}"  # noqa: N815
    imageTitleTemplate: str = "{{ @ }}"  # noqa: N815
    linkUrlTemplate: str = "{{ @ }}"  # noqa: N815
    linkTextTemplate: str = "{{ @ }}"  # noqa: N815
    linkTitleTemplate: str = "{{ @ }}"  # noqa: N815
    linkOpenInNewTab: bool = True  # noqa: N815
    displayAs: str = "string"  # noqa: N815
    visible: bool = True
    order: int = 100000
    allowSearch: bool = False  # noqa: N815
    alignContent: str = "left"  # noqa: N815
    allowHTML: bool = False  # noqa: N815
    highlightLinks: bool = False  # noqa: N815
    useMonospaceFont: bool = False  # noqa: N815
    preserveWhitespace: bool = False  # noqa: N815

    def as_dict(self):
        return dataclasses.asdict(self)


class DashboardFromFiles:
    def __init__(
        self,
        ws: WorkspaceClient,
        local_folder: Path,
        remote_folder: str,
        name_prefix: str,
        query_text_callback: Callable[[str], str] | None = None,
        warehouse_id: str | None = None,
    ):
        self._ws = ws
        self._local_folder = local_folder
        self._remote_folder = remote_folder
        self._name_prefix = name_prefix
        self._query_text_callback = query_text_callback
        self._warehouse_id = warehouse_id
        self._state = {}
        self._pos = 0

    @property
    def _query_state(self):
        return f"{self._remote_folder}/state.json"

    def dashboard_link(self, dashboard_ref: str):
        dashboard_id = self._state[f"{dashboard_ref}:dashboard_id"]
        return f"{self._ws.config.host}/sql/dashboards/{dashboard_id}"

    def create_dashboards(self) -> dict:
        dashboards = {}
        queries_per_dashboard = {}
        # Iterate over dashboards for each step, represented as first-level folders
        step_folders = [f for f in self._local_folder.glob("*") if f.is_dir()]
        for step_folder in step_folders:
            logger.debug(f"Reading step folder {step_folder}...")
            dashboard_folders = [f for f in step_folder.glob("*") if f.is_dir()]
            # Create separate dashboards per step, represented as second-level folders
            for dashboard_folder in dashboard_folders:
                logger.debug(f"Reading dashboard folder {dashboard_folder}...")
                main_name = step_folder.stem.title()
                sub_name = dashboard_folder.stem.title()
                dashboard_name = f"{self._name_prefix} {main_name} ({sub_name})"
                dashboard_ref = f"{step_folder.stem}_{dashboard_folder.stem}".lower()
                logger.info(f"Creating dashboard {dashboard_name}...")
                desired_queries = self._desired_queries(dashboard_folder, dashboard_ref)
                parent_folder_id = self._installed_query_state()
                data_source_id = self._dashboard_data_source()
                self._install_dashboard(dashboard_name, parent_folder_id, dashboard_ref)
                for query in desired_queries:
                    self._install_query(query, dashboard_name, data_source_id, parent_folder_id)
                    self._install_viz(query)
                    self._install_widget(query, dashboard_ref)
                queries_per_dashboard[dashboard_ref] = desired_queries
                dashboards[dashboard_ref] = self._state[f"{dashboard_ref}:dashboard_id"]
        self._store_query_state(queries_per_dashboard)
        return dashboards

    def validate(self):
        step_folders = [f for f in self._local_folder.glob("*") if f.is_dir()]
        for step_folder in step_folders:
            logger.info(f"Reading step folder {step_folder}...")
            dashboard_folders = [f for f in step_folder.glob("*") if f.is_dir()]
            # Create separate dashboards per step, represented as second-level folders
            for dashboard_folder in dashboard_folders:
                dashboard_ref = f"{step_folder.stem}_{dashboard_folder.stem}".lower()
                for query in self._desired_queries(dashboard_folder, dashboard_ref):
                    try:
                        self._get_viz_options(query)
                        self._get_widget_options(query)
                    except Exception as err:
                        msg = f"Error in {query.name}: {err}"
                        raise AssertionError(msg) from err

    def _install_widget(self, query: SimpleQuery, dashboard_ref: str):
        dashboard_id = self._state[f"{dashboard_ref}:dashboard_id"]
        widget_options = self._get_widget_options(query)
        # widgets are cleaned up every dashboard redeploy
        widget = self._ws.dashboard_widgets.create(
            dashboard_id, widget_options, 1, visualization_id=self._state[query.viz_key]
        )
        self._state[query.widget_key] = widget.id

    def _get_widget_options(self, query: SimpleQuery):
        self._pos += 1
        widget_options = WidgetOptions(
            title=query.widget.get("title", ""),
            description=query.widget.get("description", None),
            position=WidgetPosition(
                col=int(query.widget.get("col", 0)),
                row=int(query.widget.get("row", self._pos)),
                size_x=int(query.widget.get("size_x", 3)),
                size_y=int(query.widget.get("size_y", 3)),
            ),
        )
        return widget_options

    def _installed_query_state(self):
        try:
            self._state = json.load(self._ws.workspace.download(self._query_state))
            to_remove = []
            for k, v in self._state.items():
                _, name = k.split(":")
                if k == "dashboard_id":
                    continue
                if name != "query_id":
                    continue
                try:
                    self._ws.queries.get(v)
                except DatabricksError:
                    to_remove.append(k)
            for key in to_remove:
                del self._state[key]
        except DatabricksError as err:
            if err.error_code != "RESOURCE_DOES_NOT_EXIST":
                raise err
            self._ws.workspace.mkdirs(self._remote_folder)
        except JSONDecodeError:
            logger.warning(f"JSON state file corrupt: {self._query_state}")
            self._state = {}  # noop
        object_info = self._ws.workspace.get_status(self._remote_folder)
        parent = f"folders/{object_info.object_id}"
        return parent

    def _store_query_state(self, queries: dict[str, list[SimpleQuery]]):
        desired_keys = []
        for ref, qrs in queries.items():
            desired_keys.append(f"{ref}:dashboard_id")
            for query in qrs:
                desired_keys.append(query.query_key)
                desired_keys.append(query.viz_key)
                desired_keys.append(query.widget_key)
        destructors = {
            "query_id": self._ws.queries.delete,
            "viz_id": self._ws.query_visualizations.delete,
            "widget_id": self._ws.dashboard_widgets.delete,
        }
        new_state = {}
        for k, v in self._state.items():
            if k in desired_keys:
                new_state[k] = v
                continue
            _, name = k.split(":")
            if name not in destructors:
                continue
            try:
                destructors[name](v)
            except DatabricksError as err:
                logger.info(f"Failed to delete {name}-{v} --- {err.error_code}")
        state_dump = json.dumps(new_state, indent=2).encode("utf8")
        self._ws.workspace.upload(self._query_state, state_dump, format=ImportFormat.AUTO, overwrite=True)

    def _install_dashboard(self, dashboard_name: str, parent_folder_id: str, dashboard_ref: str):
        dashboard_id = f"{dashboard_ref}:dashboard_id"
        if dashboard_id in self._state:
            for widget in self._ws.dashboards.get(self._state[dashboard_id]).widgets:
                self._ws.dashboard_widgets.delete(widget.id)
            return
        dash = self._ws.dashboards.create(dashboard_name, run_as_role=RunAsRole.VIEWER, parent=parent_folder_id)
        self._ws.dbsql_permissions.set(
            ObjectTypePlural.DASHBOARDS,
            dash.id,
            access_control_list=[AccessControl(group_name="users", permission_level=PermissionLevel.CAN_VIEW)],
        )
        self._state[dashboard_id] = dash.id

    def _desired_queries(self, local_folder: Path, dashboard_ref: str) -> list[SimpleQuery]:
        desired_queries = []
        for f in local_folder.glob("*.sql"):
            text = f.read_text("utf8")
            if self._query_text_callback is not None:
                text = self._query_text_callback(text)
            desired_queries.append(
                SimpleQuery(
                    dashboard_ref=dashboard_ref,
                    name=f.name,
                    query=text,
                    viz=self._parse_magic_comment(f, "-- viz ", text),
                    widget=self._parse_magic_comment(f, "-- widget ", text),
                )
            )
        return desired_queries

    def _install_viz(self, query: SimpleQuery):
        viz_args = self._get_viz_options(query)
        if query.viz_key in self._state:
            return self._ws.query_visualizations.update(self._state[query.viz_key], **viz_args)
        viz = self._ws.query_visualizations.create(self._state[query.query_key], **viz_args)
        self._state[query.viz_key] = viz.id

    def _get_viz_options(self, query: SimpleQuery):
        viz_types = {"table": self._table_viz_args, "counter": self._counter_viz_args}
        if query.viz_type not in viz_types:
            msg = f"{query.query}: unknown viz type: {query.viz_type}"
            raise SyntaxError(msg)
        viz_args = viz_types[query.viz_type](**query.viz_args)
        return viz_args

    def _install_query(self, query: SimpleQuery, dashboard_name: str, data_source_id: str, parent: str):
        query_meta = {
            "data_source_id": data_source_id,
            "name": f"{dashboard_name} - {query.name}",
            "query": query.query,
        }
        if query.query_key in self._state:
            return self._ws.queries.update(self._state[query.query_key], **query_meta)

        deployed_query = self._ws.queries.create(parent=parent, run_as_role=RunAsRole.VIEWER, **query_meta)
        self._ws.dbsql_permissions.set(
            ObjectTypePlural.QUERIES,
            deployed_query.id,
            access_control_list=[AccessControl(group_name="users", permission_level=PermissionLevel.CAN_RUN)],
        )
        self._state[query.query_key] = deployed_query.id

    @staticmethod
    def _table_viz_args(
        name: str,
        columns: str,
        *,
        items_per_page: int = 25,
        condensed=True,
        with_row_number=False,
        description: str | None = None,
    ) -> dict:
        return {
            "type": "TABLE",
            "name": name,
            "description": description,
            "options": {
                "itemsPerPage": items_per_page,
                "condensed": condensed,
                "withRowNumber": with_row_number,
                "version": 2,
                "columns": [VizColumn(name=x, title=x).as_dict() for x in columns.split(",")],
            },
        }

    @staticmethod
    def _counter_viz_args(
        name: str,
        value_column: str,
        *,
        description: str | None = None,
        counter_label: str | None = None,
        value_row_number: int = 1,
        target_row_number: int = 1,
        string_decimal: int = 0,
        string_decimal_char: str = ".",
        string_thousand_separator: str = ",",
        tooltip_format: str = "0,0.000",
        count_row: bool = False,
    ) -> dict:
        return {
            "type": "COUNTER",
            "name": name,
            "description": description,
            "options": {
                "counterLabel": counter_label,
                "counterColName": value_column,
                "rowNumber": value_row_number,
                "targetRowNumber": target_row_number,
                "stringDecimal": string_decimal,
                "stringDecChar": string_decimal_char,
                "stringThouSep": string_thousand_separator,
                "tooltipFormat": tooltip_format,
                "countRow": count_row,
            },
        }

    @staticmethod
    def _parse_magic_comment(f, magic_comment, text):
        viz_comment = next(_ for _ in text.splitlines() if _.startswith(magic_comment))
        if not viz_comment:
            msg = f'{f}: cannot find "{magic_comment}" magic comment'
            raise SyntaxError(msg)
        return dict(_.split("=") for _ in viz_comment.replace(magic_comment, "").split(", "))

    def _dashboard_data_source(self) -> str:
        data_sources = {_.warehouse_id: _.id for _ in self._ws.data_sources.list()}
        warehouses = self._ws.warehouses.list()
        warehouse_id = self._warehouse_id
        if not warehouse_id and not warehouses:
            msg = "need either configured warehouse_id or an existing SQL warehouse"
            raise ValueError(msg)
        if not warehouse_id:
            warehouse_id = warehouses[0].id
        data_source_id = data_sources[warehouse_id]
        return data_source_id
