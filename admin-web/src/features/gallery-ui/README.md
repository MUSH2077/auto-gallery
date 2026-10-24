# Gallery components

Shared controls, collection views, details, navigation and login presentation accept values, links and callbacks. Business controllers in features/gallery use the existing authentication, permissions, query, preferences, media and task-command services. Mount GalleryShell and the page controllers under the existing application Providers. GalleryRoot imports the scoped application stylesheet and supplies presentation language and typography.

The wordmark uses Italianno. Latin text and numerals use the full variable Fraunces face; Chinese headings use Noto Serif SC and functional text uses LXGW WenKai. The text roles are 14/22px metadata, 16/26px controls, 18/28px body/task titles, 22/32px work/state titles, 28/38px dialog titles and 33/44px login titles. The wordmark uses 40px in navigation and 64px on login. Font files and licenses are local to this module.

Theme selection and saved language/appearance preferences use the application's existing providers. The gallery stylesheet scopes its dark tokens to the gallery root. Existing explicit view preferences take priority over the masonry default. Selection mode is transient. The wordmark is a current-page link; ordinary activation retains the URL and state.

Component copy is Chinese by default and supports English through GalleryCopyProvider. Domain messages use the existing application dictionaries. User content is not translated. Keep presentation components free of API clients and asynchronous command state.

These modules do not register application routes. defaultGalleryRoutes refers to the existing admin routes; GalleryShell and LoginPage accept the route values they need from their owner.
