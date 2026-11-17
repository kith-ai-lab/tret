import type { PackageMapPayload, PackageMapUnit } from '../../api/client'

/** What a page map's numbers count. A map made before the ESEF filing fallback
 *  carries no `unit`: that is "pdf_page". */
export function mapUnit(payload: Pick<PackageMapPayload, 'map' | 'source'>): PackageMapUnit {
  const u = payload.map?.unit ?? payload.source?.unit
  return u === 'filing_page' || u === 'filing_section' ? u : 'pdf_page'
}

export function isFilingUnit(unit: PackageMapUnit | undefined | null): boolean {
  return unit === 'filing_page' || unit === 'filing_section'
}

/** The visible label on anything built from a regulated ESEF filing. */
export const FILING_LABEL = "From the regulated ESEF filing (not the issuer's PDF)"

/** "page" / "filing page" / "section", pluralised for `n` (when given). */
export function unitNoun(unit: PackageMapUnit, n = 2): string {
  const one = unit === 'filing_page' ? 'filing page' : unit === 'filing_section' ? 'section' : 'page'
  return n === 1 ? one : `${one}s`
}

export function rangeText([first, last]: [number, number]): string {
  return first === last ? `${first}` : `${first}–${last}`
}

/** Capitalised unit label for a set of ranges: "Pages", "Filing pages", "Sections". */
function unitLabel(unit: PackageMapUnit, singular: boolean): string {
  const noun = unitNoun(unit, singular ? 1 : 2)
  return noun.charAt(0).toUpperCase() + noun.slice(1)
}

/** The ranges as one phrase, e.g. "Filing pages 210–268, 300–302" or "Sections 14–19". */
export function rangesText(unit: PackageMapUnit, ranges: [number, number][]): string {
  const singular = ranges.length === 1 && ranges[0][0] === ranges[0][1]
  return `${unitLabel(unit, singular)} ${ranges.map(rangeText).join(', ')}`
}

/** The selection of a filing map: "Filing pages 210–268 of 412" / "Sections 14–19 of 52"
 *  (whole filing: "Whole filing · 412 filing pages"). */
export function filingSelectionText(payload: PackageMapPayload): string {
  const unit = mapUnit(payload)
  const total = payload.source.unit_count ?? payload.source.page_count
  const ranges = payload.map.keep_ranges
  if (payload.full_original || !ranges) {
    const n = total ?? payload.selected_page_count
    return `Whole filing · ${n} ${unitNoun(unit, n)}`
  }
  return `${rangesText(unit, ranges)}${total != null ? ` of ${total}` : ''}`
}
