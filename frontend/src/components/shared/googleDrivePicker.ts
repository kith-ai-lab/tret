/** Google Picker integration for "Import from Google Drive" (Documents.tsx).
 *
 *  Deliberately not a dependency: `gapi`/the Picker API are loaded on demand
 *  from https://apis.google.com/js/api.js only when a user actually clicks
 *  "Google Drive" — nothing about this module runs, or adds to the bundle,
 *  for a workspace that never uses the feature. The globals below are typed
 *  just enough to call the handful of methods this file needs; there is no
 *  `@types/google.picker` package installed, by design (see the module's own
 *  callers for why: this is the one place those globals are touched).
 */

interface GooglePickerDoc {
  id: string
  name: string
  mimeType?: string
}

interface GooglePickerCallbackData {
  action: string
  docs?: GooglePickerDoc[]
}

interface GooglePickerInstance {
  setVisible: (visible: boolean) => void
}

interface GooglePickerBuilder {
  addView: (view: unknown) => GooglePickerBuilder
  enableFeature: (feature: string) => GooglePickerBuilder
  setOAuthToken: (token: string) => GooglePickerBuilder
  setDeveloperKey: (key: string) => GooglePickerBuilder
  setAppId: (appId: string) => GooglePickerBuilder
  setCallback: (cb: (data: GooglePickerCallbackData) => void) => GooglePickerBuilder
  build: () => GooglePickerInstance
}

interface GoogleDocsView {
  setIncludeFolders?: (include: boolean) => GoogleDocsView
}

declare global {
  interface Window {
    gapi?: {
      load: (api: string, opts: { callback: () => void; onerror?: () => void }) => void
    }
    google?: {
      picker: {
        PickerBuilder: new () => GooglePickerBuilder
        DocsView: new (viewId?: string) => GoogleDocsView
        ViewId: {
          DOCUMENTS: string
          SPREADSHEETS: string
          PDFS: string
          DOCS: string
        }
        Action: { PICKED: string; CANCEL: string }
        Feature: { MULTISELECT_ENABLED: string }
      }
    }
  }
}

const GAPI_SCRIPT_SRC = 'https://apis.google.com/js/api.js'

let gapiScriptPromise: Promise<void> | null = null

/** Injects the gapi bootstrap script exactly once, however many times this is
 *  called — a second "Google Drive" click while the first load is still in
 *  flight reuses the same promise rather than appending a second `<script>`. */
function loadGapiScript(): Promise<void> {
  if (window.gapi) return Promise.resolve()
  if (gapiScriptPromise) return gapiScriptPromise
  gapiScriptPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = GAPI_SCRIPT_SRC
    script.async = true
    script.defer = true
    script.onload = () => resolve()
    script.onerror = () => {
      gapiScriptPromise = null // let a retry re-attempt the load
      reject(new Error('Could not load the Google API script.'))
    }
    document.head.appendChild(script)
  })
  return gapiScriptPromise
}

function loadPickerModule(): Promise<void> {
  return new Promise((resolve, reject) => {
    if (!window.gapi) {
      reject(new Error('Google API script did not initialize.'))
      return
    }
    window.gapi.load('picker', {
      callback: () => resolve(),
      onerror: () => reject(new Error('Could not load the Google Picker.')),
    })
  })
}

export interface GooglePickerConfig {
  api_key: string
  app_id: string
}

export interface PickedFile {
  id: string
  name: string
  drive_id: null
}

/** Opens the Google Picker over Docs, Sheets, PDFs, and every other file
 *  type, multi-select enabled, and resolves with whatever the user picked
 *  (an empty array on cancel). Every failure mode — the script won't load,
 *  the token endpoint 404s or errors, the Picker itself won't load — rejects
 *  with a plain Error rather than throwing something the caller has to guess
 *  the shape of, so a caller can show it as a notice instead of crashing. */
export async function openGooglePicker(
  config: GooglePickerConfig,
  getAccessToken: () => Promise<string>,
): Promise<PickedFile[]> {
  await loadGapiScript()
  await loadPickerModule()

  const google = window.google
  if (!google) throw new Error('The Google Picker did not initialize.')

  const token = await getAccessToken()

  return new Promise((resolve, reject) => {
    try {
      const { picker } = google
      const builder = new picker.PickerBuilder()
        .addView(new picker.DocsView(picker.ViewId.DOCUMENTS))
        .addView(new picker.DocsView(picker.ViewId.SPREADSHEETS))
        .addView(new picker.DocsView(picker.ViewId.PDFS))
        .addView(new picker.DocsView(picker.ViewId.DOCS))
        .enableFeature(picker.Feature.MULTISELECT_ENABLED)
        .setOAuthToken(token)
        .setDeveloperKey(config.api_key)
        .setAppId(config.app_id)
        .setCallback((data: GooglePickerCallbackData) => {
          if (data.action === picker.Action.PICKED) {
            resolve((data.docs ?? []).map((d) => ({ id: d.id, name: d.name, drive_id: null })))
          } else if (data.action === picker.Action.CANCEL) {
            resolve([])
          }
        })
      builder.build().setVisible(true)
    } catch {
      reject(new Error('Could not open the Google Picker.'))
    }
  })
}
