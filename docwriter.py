from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

DEFAULT_SCOPES = [
	"https://www.googleapis.com/auth/documents",
	"https://www.googleapis.com/auth/drive.file",
]


class DocWriter:
	def __init__(
		self,
		credentials_path: str = "credentials.json",
		token_path: str = "token.json",
		scopes: Optional[Iterable[str]] = None,
	) -> None:
		self.credentials_path = Path(credentials_path)
		self.token_path = Path(token_path)
		self.scopes = list(scopes) if scopes else list(DEFAULT_SCOPES)

	def _load_credentials(self) -> Credentials:
		creds: Optional[Credentials] = None
		if self.token_path.exists():
			creds = Credentials.from_authorized_user_file(self.token_path, self.scopes)

		if creds and creds.expired and creds.refresh_token:
			creds.refresh(Request())

		if not creds or not creds.valid:
			flow = InstalledAppFlow.from_client_secrets_file(
				str(self.credentials_path),
				self.scopes,
			)
			creds = flow.run_local_server(host="localhost", port=8080, open_browser=False)

		self.token_path.write_text(creds.to_json(), encoding="utf-8")
		return creds

	def _docs_service(self):
		creds = self._load_credentials()
		return build("docs", "v1", credentials=creds)

	def create_document(self, title: str) -> dict:
		service = self._docs_service()
		return service.documents().create(body={"title": title}).execute()

	def replace_text(self, document_id: str, replacements: List[dict]) -> dict:
		requests = []
		for item in replacements:
			find_text = item.get("find", "")
			replace_text = item.get("replace", "")
			if not find_text:
				continue
			requests.append(
				{
					"replaceAllText": {
						"containsText": {"text": find_text, "matchCase": True},
						"replaceText": replace_text,
					}
				}
			)

		if not requests:
			return {"status": "no-op"}

		service = self._docs_service()
		return (
			service.documents()
			.batchUpdate(documentId=document_id, body={"requests": requests})
			.execute()
		)

	def append_text(self, document_id: str, text: str) -> dict:
		if not text:
			return {"status": "no-op"}

		service = self._docs_service()
		document = service.documents().get(documentId=document_id).execute()
		end_index = _get_document_end_index(document)
		requests = [
			{
				"insertText": {
					"location": {"index": end_index},
					"text": text,
				}
			}
		]
		return (
			service.documents()
			.batchUpdate(documentId=document_id, body={"requests": requests})
			.execute()
		)

	def replace_document_text(self, document_id: str, text: str) -> dict:
		service = self._docs_service()
		document = service.documents().get(documentId=document_id).execute()
		end_index = _get_document_end_index(document)
		requests = []
		if end_index > 2:
			requests.append(
				{
					"deleteContentRange": {
						"range": {"startIndex": 1, "endIndex": end_index - 1}
					}
				}
			)
		if text:
			requests.append(
				{
					"insertText": {"location": {"index": 1}, "text": text}
				}
			)
		if not requests:
			return {"status": "no-op"}
		return (
			service.documents()
			.batchUpdate(documentId=document_id, body={"requests": requests})
			.execute()
		)


def _get_document_end_index(document: dict) -> int:
	body = document.get("body", {})
	content = body.get("content", [])
	if not content:
		return 1
	last = content[-1]
	return max(1, int(last.get("endIndex", 1)))


def save_credentials_snapshot(credentials_path: str, output_path: str) -> None:
	creds_path = Path(credentials_path)
	payload = json.loads(creds_path.read_text(encoding="utf-8"))
	snapshot_path = Path(output_path)
	snapshot_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
