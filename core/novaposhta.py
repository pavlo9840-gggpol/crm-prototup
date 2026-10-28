"""Server-side Nova Poshta API helpers. API credentials are read only from environment."""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://api.novaposhta.ua/v2.0/json/"


class NovaPoshtaError(Exception):
    pass


def call(model, method, properties):
    key = os.environ.get("NOVA_POSHTA_API_KEY", "").strip()
    if not key:
        raise NovaPoshtaError("Не налаштовано NOVA_POSHTA_API_KEY у змінних середовища.")
    payload = {
        "apiKey": key,
        "modelName": model,
        "calledMethod": method,
        "methodProperties": properties,
    }
    request = Request(API_URL, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json", "Accept": "application/json"},
                      method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise NovaPoshtaError("Не вдалося зв'язатися з API Нової пошти. Перевірте мережу та налаштування.") from exc
    if not result.get("success"):
        errors = result.get("errors") or result.get("warnings") or ["Невідома помилка API."]
        raise NovaPoshtaError("; ".join(str(x) for x in errors)[:450])
    return result.get("data") or []


def _first(data, description):
    if not data or not isinstance(data, list) or not isinstance(data[0], dict):
        raise NovaPoshtaError(f"API не повернуло дані: {description}.")
    return data[0]


def create_ttn(shipment):
    """Create recipient/contact and an Internet Document; sender refs are configured server-side."""
    import os
    required = {
        "NP_SENDER_CITY_REF": "місто відправника",
        "NP_SENDER_REF": "контрагент відправника",
        "NP_SENDER_ADDRESS_REF": "відділення відправника",
        "NP_SENDER_CONTACT_REF": "контакт відправника",
        "NP_SENDER_PHONE": "телефон відправника",
    }
    missing = [label for name, label in required.items() if not os.environ.get(name, "").strip()]
    if missing:
        raise NovaPoshtaError("Не налаштовані змінні відправника: " + ", ".join(missing) + ".")
    if not shipment.recipient_city_ref or not shipment.recipient_warehouse_ref:
        raise NovaPoshtaError("Для створення ТТН потрібні Ref міста та відділення одержувача з довідника Нової пошти.")
    phone = "".join(ch for ch in shipment.recipient_phone if ch.isdigit())
    if len(phone) < 10:
        raise NovaPoshtaError("Перевірте телефон одержувача.")
    names = shipment.recipient_name.strip().split()
    first_name = names[0]
    last_name = names[-1] if len(names) > 1 else names[0]
    middle_name = " ".join(names[1:-1]) if len(names) > 2 else ""
    recipient = _first(call("Counterparty", "save", {
        "CounterpartyType": "PrivatePerson", "CounterpartyProperty": "Recipient",
        "FirstName": first_name, "MiddleName": middle_name, "LastName": last_name,
        "Phone": phone, "Email": "",
    }), "створення одержувача")
    contact = _first(call("ContactPerson", "save", {
        "CounterpartyRef": recipient.get("Ref", ""),
        "FirstName": first_name, "LastName": last_name, "Phone": phone,
    }), "створення контакту одержувача")
    props = {
        "PayerType": "Recipient", "PaymentMethod": "Cash", "DateTime": "",
        "CargoType": "Cargo", "VolumeGeneral": "0.001", "Weight": str(shipment.weight),
        "ServiceType": "WarehouseWarehouse", "SeatsAmount": str(shipment.seats_amount),
        "Description": shipment.description, "Cost": str(shipment.declared_cost),
        "CitySender": os.environ["NP_SENDER_CITY_REF"], "Sender": os.environ["NP_SENDER_REF"],
        "SenderAddress": os.environ["NP_SENDER_ADDRESS_REF"],
        "ContactSender": os.environ["NP_SENDER_CONTACT_REF"], "SendersPhone": os.environ["NP_SENDER_PHONE"],
        "CityRecipient": shipment.recipient_city_ref, "Recipient": recipient.get("Ref", ""),
        "RecipientAddress": shipment.recipient_warehouse_ref, "ContactRecipient": contact.get("Ref", ""),
        "RecipientsPhone": phone,
    }
    if shipment.cod_amount > 0:
        props["BackwardDeliveryData"] = [{
            "PayerType": "Recipient", "CargoType": "Money", "RedeliveryString": str(shipment.cod_amount)
        }]
    doc = _first(call("InternetDocument", "save", props), "створення експрес-накладної")
    ttn = str(doc.get("IntDocNumber") or "").strip()
    if not ttn:
        raise NovaPoshtaError("API не повернуло номер ТТН.")
    return ttn


def track_ttn(ttn, phone=""):
    props = [{"DocumentNumber": ttn, "Phone": "".join(ch for ch in phone if ch.isdigit())}]
    return _first(call("TrackingDocument", "getStatusDocuments", {"Documents": props}), "відстеження ТТН")
