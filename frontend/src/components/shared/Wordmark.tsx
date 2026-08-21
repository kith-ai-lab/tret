/** The tret wordmark from the launch site: "tre", the copper slash, "t".
 *  Sized in em so one set of CSS rules serves the sidebar and the login
 *  card; pass `size` (px) per placement. The slash gradient is the brand
 *  constant `--brand-slash` — identical in both themes on purpose. */
export function Wordmark({ size = 20 }: { size?: number }) {
  return (
    <span className="wordmark" aria-label="tret" style={{ fontSize: size }}>
      <span className="wm-text">tre</span>
      <span className="wm-slash-box">
        <span className="wm-slash" />
      </span>
      <span className="wm-text">t</span>
    </span>
  )
}
