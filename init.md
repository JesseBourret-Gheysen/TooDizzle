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


