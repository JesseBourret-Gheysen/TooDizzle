Create a python-flask PWA app which is hosted locally on this server, and not broadcast outside of the local network. The app is similar to a todo app, but with a number of differences. 

App should have no login or user auth. It should be very simple.
There should be 2 pages.
1 for inputting things, and one for checking them off.
The app will be used from tablet, phone, and PC.

On page 1, the input page:
	- A single text box where things can be written or typed.
	- a dropdown selector with the following options for 'type':
		- Tech
		- Bio/Health
		- Exercise
		- Other
	- A submit button.

On page 2, the task page:
	- a display of all todo items, with a sortable filter based on type and age.
	- if the task has a link in it, the link should be clickable.
	- if there is a link, the base url should be extracted and put in another column which is also sortable and filterable. The column can be called source.


---

## REST API

All endpoints are under `/api/tasks`. Responses are JSON. No authentication required (local network only).

### CRUD

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/tasks` | List all tasks |
| GET | `/api/tasks/<id>` | Get a single task by ID |
| POST | `/api/tasks` | Create a new task |
| PUT | `/api/tasks/<id>` | Update an existing task |
| DELETE | `/api/tasks/<id>` | Delete a task (returns 204) |

### Filtered GET endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/tasks/type/<task_type>` | All tasks matching a type exactly (case-insensitive). e.g. `/api/tasks/type/Tech` |
| GET | `/api/tasks/title/<query>` | All tasks whose text contains the query string (case-insensitive substring). e.g. `/api/tasks/title/docker` |

### Request body fields (POST / PUT)

| Field | Required | Notes |
|-------|----------|-------|
| `text` | Yes (POST) | Task text content |
| `type` | No | `Tech`, `Bio/Health`, `Exercise`, `Other` (default: `Other`) |
| `subtype` | No | Only applied when `type` is `Tech` |
| `done` | No | `true`/`false` — PUT only |

### Response object

```json
{
  "id": "a1b2c3d4",
  "text": "Task text here",
  "type": "Tech",
  "subtype": "Python",
  "date_added": "2026-04-07",
  "done": ""
}
```

`done` is `"yes"` when complete, `""` when active.
