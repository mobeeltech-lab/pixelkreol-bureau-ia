"""Génération du XML Factur-X (CII, profil BASIC WL) — validé contre le XSD embarqué dans la lib factur-x."""
from xml.sax.saxutils import escape as e

NS = ('xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100" '
      'xmlns:qdt="urn:un:unece:uncefact:data:standard:QualifiedDataType:100" '
      'xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100" '
      'xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"')


def _m(x: float) -> str:
    return f"{x:.2f}"


def _d(iso: str) -> str:
    return iso.replace("-", "")[:8]


def generer(p: dict, vendeur: dict) -> str:
    """p : pièce calculée (numero, type, date, echeance, client, ventilation, totaux, acompte)."""
    type_code = "381" if p["type"] == "avoir" else "380"
    c = p["client"]
    s = []
    s.append(f'<?xml version="1.0" encoding="UTF-8"?>\n<rsm:CrossIndustryInvoice {NS}>')
    s.append("<rsm:ExchangedDocumentContext><ram:GuidelineSpecifiedDocumentContextParameter>"
             "<ram:ID>urn:factur-x.eu:1p0:basicwl</ram:ID></ram:GuidelineSpecifiedDocumentContextParameter></rsm:ExchangedDocumentContext>")
    s.append(f"<rsm:ExchangedDocument><ram:ID>{e(p['numero'])}</ram:ID><ram:TypeCode>{type_code}</ram:TypeCode>"
             f'<ram:IssueDateTime><udt:DateTimeString format="102">{_d(p["date"])}</udt:DateTimeString></ram:IssueDateTime>'
             "</rsm:ExchangedDocument>")
    s.append("<rsm:SupplyChainTradeTransaction>")
    # Accord : vendeur / acheteur
    s.append("<ram:ApplicableHeaderTradeAgreement>")
    if c.get("reference"):
        s.append(f"<ram:BuyerReference>{e(c['reference'])}</ram:BuyerReference>")
    s.append(f"<ram:SellerTradeParty><ram:Name>{e(vendeur['nom'])}</ram:Name>")
    if vendeur.get("siren"):
        s.append(f'<ram:SpecifiedLegalOrganization><ram:ID schemeID="0002">{e(vendeur["siren"])}</ram:ID></ram:SpecifiedLegalOrganization>')
    s.append("<ram:PostalTradeAddress>")
    if vendeur.get("cp_ville"):
        cp = vendeur["cp_ville"].split(" ", 1)
        s.append(f"<ram:PostcodeCode>{e(cp[0])}</ram:PostcodeCode>")
    if vendeur.get("adresse"):
        s.append(f"<ram:LineOne>{e(vendeur['adresse'])}</ram:LineOne>")
    if vendeur.get("cp_ville") and " " in vendeur["cp_ville"]:
        s.append(f"<ram:CityName>{e(vendeur['cp_ville'].split(' ', 1)[1])}</ram:CityName>")
    s.append(f"<ram:CountryID>{e(vendeur.get('pays') or 'FR')}</ram:CountryID></ram:PostalTradeAddress>")
    if vendeur.get("email"):
        s.append(f'<ram:URIUniversalCommunication><ram:URIID schemeID="EM">{e(vendeur["email"])}</ram:URIID></ram:URIUniversalCommunication>')
    if vendeur.get("tva"):
        s.append(f'<ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{e(vendeur["tva"])}</ram:ID></ram:SpecifiedTaxRegistration>')
    s.append("</ram:SellerTradeParty>")
    s.append(f"<ram:BuyerTradeParty><ram:Name>{e(c['nom'])}</ram:Name>")
    if c.get("siren"):
        s.append(f'<ram:SpecifiedLegalOrganization><ram:ID schemeID="0002">{e(c["siren"])}</ram:ID></ram:SpecifiedLegalOrganization>')
    s.append(f"<ram:PostalTradeAddress><ram:CountryID>{e(c.get('pays') or 'FR')}</ram:CountryID></ram:PostalTradeAddress>")
    if c.get("tva"):
        s.append(f'<ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{e(c["tva"])}</ram:ID></ram:SpecifiedTaxRegistration>')
    s.append("</ram:BuyerTradeParty>")
    if p.get("ref"):
        s.append(f"<ram:BuyerOrderReferencedDocument><ram:IssuerAssignedID>{e(p['ref'])}</ram:IssuerAssignedID></ram:BuyerOrderReferencedDocument>")
    s.append("</ram:ApplicableHeaderTradeAgreement>")
    s.append("<ram:ApplicableHeaderTradeDelivery/>")
    # Règlement
    s.append("<ram:ApplicableHeaderTradeSettlement><ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>")
    if vendeur.get("iban"):
        s.append("<ram:SpecifiedTradeSettlementPaymentMeans><ram:TypeCode>58</ram:TypeCode>"
                 f"<ram:PayeePartyCreditorFinancialAccount><ram:IBANID>{e(vendeur['iban'].replace(' ', ''))}</ram:IBANID>"
                 "</ram:PayeePartyCreditorFinancialAccount></ram:SpecifiedTradeSettlementPaymentMeans>")
    for v in p["ventilation"]:
        s.append("<ram:ApplicableTradeTax>")
        s.append(f"<ram:CalculatedAmount>{_m(v['tva'])}</ram:CalculatedAmount><ram:TypeCode>VAT</ram:TypeCode>")
        if v["taux"] == 0:
            s.append(f"<ram:ExemptionReason>{e(p.get('motif_exoneration') or 'TVA non applicable, art. 293 B du CGI')}</ram:ExemptionReason>")
        s.append(f"<ram:BasisAmount>{_m(v['base'])}</ram:BasisAmount>")
        s.append(f"<ram:CategoryCode>{'E' if v['taux'] == 0 else 'S'}</ram:CategoryCode>")
        if v["taux"] == 0:
            s.append("<ram:ExemptionReasonCode>VATEX-FR-FRANCHISE</ram:ExemptionReasonCode>")
        s.append(f"<ram:RateApplicablePercent>{v['taux']:.2f}</ram:RateApplicablePercent></ram:ApplicableTradeTax>")
    if p.get("echeance"):
        s.append(f'<ram:SpecifiedTradePaymentTerms><ram:DueDateDateTime><udt:DateTimeString format="102">{_d(p["echeance"])}'
                 "</udt:DateTimeString></ram:DueDateDateTime></ram:SpecifiedTradePaymentTerms>")
    t = p["totaux"]
    s.append("<ram:SpecifiedTradeSettlementHeaderMonetarySummation>"
             f"<ram:LineTotalAmount>{_m(t['ht'])}</ram:LineTotalAmount>"
             f"<ram:TaxBasisTotalAmount>{_m(t['ht'])}</ram:TaxBasisTotalAmount>"
             f'<ram:TaxTotalAmount currencyID="EUR">{_m(t["tva"])}</ram:TaxTotalAmount>'
             f"<ram:GrandTotalAmount>{_m(t['ttc'])}</ram:GrandTotalAmount>"
             f"<ram:TotalPrepaidAmount>{_m(t['acompte'])}</ram:TotalPrepaidAmount>"
             f"<ram:DuePayableAmount>{_m(t['a_payer'])}</ram:DuePayableAmount>"
             "</ram:SpecifiedTradeSettlementHeaderMonetarySummation>")
    s.append("</ram:ApplicableHeaderTradeSettlement></rsm:SupplyChainTradeTransaction></rsm:CrossIndustryInvoice>")
    return "".join(s)
