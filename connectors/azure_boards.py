"""Find or create Azure Boards work items."""
import base64
import html
from urllib.parse import quote

from common import TriageError, request, required



class AzureBoards:
    def __init__(self, settings, apply):
        org = required(settings, "AZDO_ORG", r"[A-Za-z0-9-]+")
        self.project = required(settings, "AZDO_PROJECT")
        self.scope = f"azure-devops/{org.lower()}/{self.project.lower()}"
        self.base = f"https://dev.azure.com/{org}/{quote(self.project, safe='')}"
        token = required(settings, "AZDO_PAT")
        encoded = base64.b64encode(f":{token}".encode()).decode()
        self.headers = {"Authorization": "Basic " + encoded}
        if apply:
            self.area = required(settings, "AZDO_AREA_PATH")
            self.iteration = required(settings, "AZDO_BACKLOG_ITERATION_PATH")
            self.item_type = required(settings, "AZDO_WORK_ITEM_TYPE") if "AZDO_WORK_ITEM_TYPE" in settings else "Bug"

    def find(self, reference):
        project = self.project.replace("'", "''")
        reference = reference.replace("'", "''")
        query = ("SELECT [System.Id] FROM WorkItems "
                 f"WHERE [System.TeamProject] = '{project}' "
                 f"AND [System.Description] CONTAINS '{reference}' "
                 "ORDER BY [System.Id]")
        data = request("Azure Boards lookup", f"{self.base}/_apis/wit/wiql?api-version=7.1&$top=1",
                       headers=self.headers, payload={"query": query})
        matches = data["workItems"]
        return work_item_id(matches[0]) if matches else None

    def create(self, reference, environment, assessment, evidence, source_link):
        detail = [f"<p>{html.escape(assessment.summary)}</p>", f"<p>Suggested priority: {assessment.priority}/4</p>",
                  f"<p>Environment: {html.escape(environment)}<br>"
                  f"Exception: {html.escape(evidence['type'])}<br>"
                  f"Sample occurred at: {html.escape(evidence['occurred_at'])}</p>",
                  f'<p><a href="{html.escape(source_link)}">Source issue</a></p>',
                  f"<p>{html.escape(reference)}</p>"]
        if evidence["http_status"]:
            detail.append(f"<p>HTTP status: {evidence['http_status']}</p>")
        if evidence["frames"]:
            frames = []
            for frame in evidence["frames"]:
                file = frame["file"] or ""
                location = f"{file}:{frame['line']}" if file and frame["line"] else file
                frames.append("<li>" + html.escape(" ".join(filter(None, [frame["function"], location]))) + "</li>")
            detail.append("<p>Application stack</p><ul>" + "".join(frames) + "</ul>")
        else:
            detail.append("<p>No sanitized application frames were available. Open the source issue to inspect the event.</p>")
        description = "".join(detail)
        fields = {"System.Title": f"[Triage/{environment}] {assessment.title}",
                  "System.Description": description, "System.AreaPath": self.area,
                  "System.IterationPath": self.iteration, "System.Tags": environment}
        if self.item_type.casefold() == "bug":
            fields["Microsoft.VSTS.TCM.ReproSteps"] = description
        patch = [{"op": "add", "path": f"/fields/{field}", "value": value} for field, value in fields.items()]
        patch.append({"op": "add", "path": "/relations/-",
                      "value": {"rel": "Hyperlink", "url": source_link, "attributes": {"comment": "Source issue"}}})
        url = f"{self.base}/_apis/wit/workitems/${quote(self.item_type, safe='')}?api-version=7.1"
        return work_item_id(request("Azure Boards create", url, headers=self.headers, payload=patch,
                                    content_type="application/json-patch+json"))

    def item_link(self, item_id):
        return f"{self.base}/_workitems/edit/{item_id}"


def work_item_id(data):
    item_id = data.get("id")
    if type(item_id) is not int or item_id < 1:
        raise TriageError("Azure Boards returned an invalid work-item ID.")
    return item_id
